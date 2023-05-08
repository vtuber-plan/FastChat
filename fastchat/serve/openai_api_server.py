"""A server that provides OpenAI-compatible RESTful APIs. It supports:

- Chat Completions. (Reference: https://platform.openai.com/docs/api-reference/chat)
- Completions. (Reference: https://platform.openai.com/docs/api-reference/completions)
- Embeddings. (Reference: https://platform.openai.com/docs/api-reference/embeddings)

Usage:
python3 -m fastchat.serve.openai_api_server
"""
import asyncio

import argparse
import asyncio
import json
import logging

import os
from typing import Generator, Optional, Union, Dict, List, Any

import fastapi
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import httpx
import shortuuid
import uvicorn
from pydantic import BaseSettings

from fastapi import HTTPException
from fastchat.constants import WORKER_API_TIMEOUT
from fastchat.model.model_adapter import get_conversation_template
from fastapi.exceptions import RequestValidationError
from fastchat.protocol.openai_api_protocol import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionResponseStreamChoice,
    ChatCompletionStreamResponse,
    ChatMessage,
    ChatCompletionResponseChoice,
    CompletionRequest,
    CompletionResponse,
    DeltaMessage,
    EmbeddingsRequest,
    EmbeddingsResponse,
    ErrorResponse,
)

logger = logging.getLogger(__name__)


class AppSettings(BaseSettings):
    # The address of the model controller.
    controller_address: str = "http://localhost:21001"


app_settings = AppSettings()

app = fastapi.FastAPI()
headers = {"User-Agent": "FastChat API Server"}

def create_error_response(code: int, message: str) -> JSONResponse:
    return JSONResponse(ErrorResponse(message=message, code=code).dict(), status_code=500) 


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    return create_error_response(50001, "Internal Serverless Error")


@app.get("/v1/models")
async def show_available_models():
    controller_address = app_settings.controller_address
    async with httpx.AsyncClient() as client:
        ret = await client.post(controller_address + "/refresh_all_workers")
        ret = await client.post(controller_address + "/list_models")
    models = ret.json()["models"]
    models.sort()
    return {"data": [{"id": m, "object": "model"} for m in models], "object": "list"}


@app.post("/v1/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest):
    """Creates a completion for the chat message"""
    # First check all params
    if request.max_tokens <= 0:
        return create_error_response(
            50099,
            f"{request.max_tokens} is less than the minimum of 1 - 'max_tokens'"
        )
    if request.n <= 0:
        return create_error_response(
            50099,
            f"{request.n} is less than the minimum of 1 - 'n'"
        )
    if request.temperature < 0:
        return create_error_response(
            50099,
            f"{request.temperature} is less than the minimum of 0 - 'temperature'"
        )
    if request.temperature > 2:
        return create_error_response(
            50098,
            f"{request.temperature} is greater than the maximum of 2 - 'temperature'"
        )
    if request.top_p < 0:
        return create_error_response(
            50099,
            f"{request.top_p} is less than the minimum of 0 - 'top_p'"
        )
    if request.top_p > 1:
        return create_error_response(
            50098,
            f"{request.top_p} is greater than the maximum of 1 - 'temperature'"
        )

    # Generate params dict
    gen_params = get_gen_params(
        request.model,
        request.messages,
        temperature=request.temperature,
        top_p=request.top_p,
        max_tokens=request.max_tokens,
        echo=False,
        stream=request.stream,
        stop=request.stop,
    )

    if request.stream:
        generator = chat_completion_stream_generator(request.model, gen_params, request.n)
        return StreamingResponse(generator, media_type="text/event-stream")

    choices = []
    # TODO: batch the requests. maybe not necessary if using CacheFlow worker
    chat_completions = []
    for i in range(request.n):
        content = asyncio.create_task(chat_completion(request.model, gen_params))
        chat_completions.append(content)

    usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    for i, content_task in enumerate(chat_completions):
        content = await content_task
        choices.append(
            ChatCompletionResponseChoice(
                index=i,
                message=ChatMessage(role="assistant", content=content["text"]),
                finish_reason=content.get("finish_reason", "stop"),
            )
        )
        for usage_k, usage_v in content["usage"].items():
            usage[usage_k] += usage_v

    return ChatCompletionResponse(model=request.model, choices=choices, usage=usage)

def get_gen_params(
    model_name: str,
    messages: List[Dict[str, str]],
    *,
    temperature: float,
    top_p: float,
    max_tokens: int,
    echo: Optional[bool],
    stream: Optional[bool],
    stop: Optional[Union[str, List[str]]],
) -> Dict[str, Any]:
    is_chatglm = "chatglm" in model_name.lower()
    conv = get_conversation_template(model_name)
    for message in messages:
        msg_role = message["role"]
        if msg_role == "system":
            conv.system = message["content"]
        elif msg_role == "user":
            conv.append_message(conv.roles[0], message["content"])
        elif msg_role == "assistant":
            conv.append_message(conv.roles[1], message["content"])
        else:
            raise ValueError(f"Unknown role: {msg_role}")

    # Add a blank message for the assistant.
    conv.append_message(conv.roles[1], None)

    if is_chatglm:
        prompt = conv.messages[conv.offset :]
    else:
        prompt = conv.get_prompt()

    if max_tokens is None:
        max_tokens = 512

    gen_params = {
        "model": model_name,
        "prompt": prompt,
        "temperature": temperature,
        "top_p": top_p,
        "max_new_tokens": max_tokens,
        "echo": echo,
        "stream": stream
    }

    if stop is None:
        gen_params.update({
            "stop": conv.stop_str,
            "stop_token_ids": conv.stop_token_ids
        })
    else:
        gen_params.update({
            "stop": stop
        })

    logger.debug(f"==== request ====\n{gen_params}")
    return gen_params


async def _get_worker_address(model_name: str, client: httpx.AsyncClient) -> str:
    """
    Get worker address based on the requested model

    :param model_name: The worker's model name
    :param client: The httpx client to use
    :return: Worker address from the controller
    :raises: :class:`ValueError`: No available worker for requested model
    """
    controller_address = app_settings.controller_address

    ret = await client.post(
        controller_address + "/get_worker_address", json={"model": model_name}
    )
    worker_addr = ret.json()["address"]
    # No available worker
    if worker_addr == "":
        raise ValueError(f"No available worker for {model_name}")

    logger.debug(f"model_name: {model_name}, worker_addr: {worker_addr}")
    return worker_addr


async def chat_completion_stream_generator(
        model_name: str,
        gen_params: Dict[str, Any],
        n: int) -> Generator[str, Any, None]:
    """
    Event stream format:
    https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events#event_stream_format
    """
    id = f"chatcmpl-{shortuuid.random()}"
    finish_stream_events = []
    for i in range(n):
        # First chunk with role
        choice_data = ChatCompletionResponseStreamChoice(
            index=i,
            delta=DeltaMessage(role="assistant"),
            finish_reason=None,
        )
        chunk = ChatCompletionStreamResponse(
            id=id,
            choices=[choice_data],
            model=model_name
        )
        yield f"data: {chunk.json(exclude_unset=True, ensure_ascii=False)}\n\n"

        previous_text = ""
        async for content in chat_completion_stream(model_name, gen_params):
            decoded_unicode = content["text"].replace('\ufffd', '')
            delta_text = decoded_unicode[len(previous_text):]
            previous_text = decoded_unicode

            if len(delta_text) == 0:
                delta_text = None
            choice_data = ChatCompletionResponseStreamChoice(
                index=i,
                delta=DeltaMessage(content=delta_text),
                finish_reason=content.get("finish_reason", None),
            )
            chunk = ChatCompletionStreamResponse(
                id=id,
                choices=[choice_data],
                model=model_name
            )
            if delta_text is None:
                if content.get("finish_reason", None) is not None:
                    finish_stream_events.append(chunk)
                continue
            yield f"data: {chunk.json(exclude_unset=True, ensure_ascii=False)}\n\n"
    # There is not "content" field in the last delta message, so exclude_none to exclude field "content".
    for finish_chunk in finish_stream_events:
        yield f"data: {finish_chunk.json(exclude_none=True, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def chat_completion_stream(model_name: str, gen_params: Dict[str, Any]):
    controller_url = app_settings.controller_address
    async with httpx.AsyncClient() as client:
        ret = await client.post(
            controller_url + "/get_worker_address", json={"model": model_name}
        )
        worker_addr = ret.json()["address"]
        # No available worker
        if worker_addr == "":
            raise ValueError(f"No available worker for {model_name}")

        logger.debug(f"model_name: {model_name}, worker_addr: {worker_addr}")

        delimiter = b"\0"
        async with client.stream(
            "POST",
            worker_addr + "/worker_generate_stream",
            headers=headers,
            json=gen_params,
            timeout=WORKER_API_TIMEOUT,
        ) as response:
            # content = await response.aread()
            async for raw_chunk in response.aiter_raw():
                for chunk in raw_chunk.split(delimiter):
                    if not chunk:
                        continue
                    data = json.loads(chunk.decode())
                    yield data

async def chat_completion(model_name: str, gen_params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        worker_addr = await _get_worker_address(model_name, client)

        output = None
        delimiter = b"\0"

        async with client.stream(
            "POST",
            worker_addr + "/worker_generate_stream",
            headers=headers,
            json=gen_params,
            timeout=WORKER_API_TIMEOUT,
        ) as response:
            content = await response.aread()

        for chunk in content.split(delimiter):
            if not chunk:
                continue
            data = json.loads(chunk.decode())
            if data["error_code"] == 0:
                output = data

        return output

@app.post("/v1/completions")
async def create_completion(request: CompletionRequest):
    payload = {
        "model": request.model,
        "prompt": request.prompt,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "logprobs": request.logprobs,
    }

    if request.stream:
        raise NotImplementedError("streaming is not supported yet")
    else:
        completions = []
        prompt_tokens = 0
        completion_tokens = 0
        for i in range(request.n):
            content = await generate_completion(payload)
            content = json.loads(content)
            content["index"] = i
            completion_tokens += content["completion_tokens"]
            prompt_tokens = content["prompt_tokens"]
            content.pop("completion_tokens")
            content.pop("prompt_tokens")
            if request.echo:
                content["text"] = request.prompt + content["text"]
            completions.append(content)
    return CompletionResponse(
        model=request.model,
        choices=completions,
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )


async def generate_completion(payload: Dict[str, Any]):
    controller_address = app_settings.controller_address
    async with httpx.AsyncClient() as client:
        worker_addr = await _get_worker_address(payload["model"], client)

        response = await client.post(
            worker_addr + "/worker_generate_completion",
            headers=headers,
            json=payload,
            timeout=WORKER_API_TIMEOUT,
        )
        completion = response.json()
        return completion


@app.post("/v1/create_embeddings")
async def create_embeddings(request: EmbeddingsRequest):
    """Creates embeddings for the text"""
    payload = {
        "model": request.model,
        "input": request.input,
    }

    embedding = await get_embedding(payload)
    embedding = json.loads(embedding)
    data = [{"object": "embedding", "embedding": embedding["embedding"], "index": 0}]
    return EmbeddingsResponse(
        data=data,
        model=request.model,
        usage={
            "prompt_tokens": embedding["token_num"],
            "total_tokens": embedding["token_num"],
        },
    )


async def get_embedding(payload: Dict[str, Any]):
    controller_address = app_settings.controller_address
    model_name = payload["model"]
    async with httpx.AsyncClient() as client:
        worker_addr = await _get_worker_address(model_name, client)

        response = await client.post(
            worker_addr + "/worker_get_embeddings",
            headers=headers,
            json=payload,
            timeout=WORKER_API_TIMEOUT,
        )
        embedding = response.json()
        return embedding


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="FastChat ChatGPT-Compatible RESTful API server."
    )
    parser.add_argument("--host", type=str, default="localhost", help="host name")
    parser.add_argument("--port", type=int, default=8000, help="port number")
    parser.add_argument(
        "--controller-address", type=str, default="http://localhost:21001"
    )
    parser.add_argument(
        "--allow-credentials", action="store_true", help="allow credentials"
    )
    parser.add_argument(
        "--allowed-origins", type=json.loads, default=["*"], help="allowed origins"
    )
    parser.add_argument(
        "--allowed-methods", type=json.loads, default=["*"], help="allowed methods"
    )
    parser.add_argument(
        "--allowed-headers", type=json.loads, default=["*"], help="allowed headers"
    )
    args = parser.parse_args()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=args.allowed_origins,
        allow_credentials=args.allow_credentials,
        allow_methods=args.allowed_methods,
        allow_headers=args.allowed_headers,
    )
    app_settings.controller_address = args.controller_address

    logger.debug(f"==== args ====\n{args}")

    uvicorn.run("fastchat.serve.openai_api_server:app", host=args.host, port=args.port, log_level="info", reload=True)
