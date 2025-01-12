import secrets
import time
from functools import partial
from typing import AsyncGenerator, List, Dict

import anyio
from fastapi import APIRouter, Request, Depends, HTTPException
from openai.types.completion import Completion
from openai.types.completion_choice import CompletionChoice, Logprobs
from openai.types.completion_usage import CompletionUsage
from sse_starlette import EventSourceResponse
from vllm.outputs import RequestOutput
from vllm.sampling_params import SamplingParams

from api.config import SETTINGS
from api.utils.protocol import CompletionCreateParams
from api.utils.request import (
    handle_request,
    get_engine,
    get_event_publisher,
    check_api_key
)
from api.vllm_routes.utils import get_model_inputs

completion_router = APIRouter()


@completion_router.post("/completions", dependencies=[Depends(check_api_key)])
async def create_completion(
    request: CompletionCreateParams,
    raw_request: Request,
    engine=Depends(get_engine),
):
    """Completion API similar to OpenAI's API.

    See https://platform.openai.com/docs/api-reference/completions/create
    for the API specification. This API mimics the OpenAI Completion API.

    NOTE: Currently we do not support the following features:
        - echo (since the vLLM engine does not currently support
          getting the logprobs of prompt tokens)
        - suffix (the language models we currently support do not support
          suffix)
        - logit_bias (to be supported by vLLM engine)
    """
    if request.echo:
        # We do not support echo since the vLLM engine does not
        # currently support getting the logprobs of prompt tokens.
        raise HTTPException(status_code=400, detail="echo is not currently supported")

    if request.suffix:
        # The language models we currently support do not support suffix.
        raise HTTPException(status_code=400, detail="suffix is not currently supported")

    request.max_tokens = request.max_tokens or 128
    request_id = f"cmpl-{secrets.token_hex(12)}"

    token_ids, error_check_ret = await get_model_inputs(engine, request, request.prompt, SETTINGS.model_name.lower())
    if error_check_ret is not None:
        return error_check_ret

    request, stop_token_ids = await handle_request(request, engine.prompt_adapter.stop, chat=False)

    try:
        sampling_params = SamplingParams(
            n=request.n,
            presence_penalty=request.presence_penalty,
            frequency_penalty=request.frequency_penalty,
            temperature=request.temperature,
            top_p=request.top_p,
            stop=request.stop,
            stop_token_ids=stop_token_ids,
            max_tokens=request.max_tokens,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result_generator = engine.generate(
        request.prompt if isinstance(request.prompt, str) else None, sampling_params, request_id, token_ids,
    )

    # Streaming response
    if request.stream:
        generator = generate_completion_stream_generator(result_generator, request, request_id, engine.engine.tokenizer)
        send_chan, recv_chan = anyio.create_memory_object_stream(10)
        return EventSourceResponse(
            recv_chan,
            data_sender_callable=partial(
                get_event_publisher,
                request=raw_request,
                inner_send_chan=send_chan,
                iterator=generator,
            ),
        )

    # Non-streaming response
    final_res: RequestOutput = None
    async for res in result_generator:
        if await raw_request.is_disconnected():
            # Abort the request if the client disconnects.
            await engine.abort(request_id)
            return
        final_res = res
        
    assert final_res is not None
    choices = []
    for output in final_res.outputs:
        output.text = output.text.replace("�", "")
        logprobs = None
        if request.logprobs is not None:
            logprobs = create_logprobs(engine.engine.tokenizer, output.token_ids, output.logprobs)
        choice = CompletionChoice(
            index=output.index,
            text=output.text,
            finish_reason=output.finish_reason,
            logprobs=logprobs,
        )
        choices.append(choice)

    num_prompt_tokens = len(final_res.prompt_token_ids)
    num_generated_tokens = sum(len(output.token_ids) for output in final_res.outputs)
    usage = CompletionUsage(
        prompt_tokens=num_prompt_tokens,
        completion_tokens=num_generated_tokens,
        total_tokens=num_prompt_tokens + num_generated_tokens,
    )

    return Completion(
        id=request_id, choices=choices, created=int(time.time()),
        model=request.model, object="text_completion", usage=usage
    )


def create_logprobs(
    tokenizer,
    token_ids: List[int],
    id_logprobs: List[Dict[int, float]],
    initial_text_offset: int = 0
) -> Logprobs:
    """Create OpenAI-style logprobs."""
    logprobs = Logprobs(text_offset=[], token_logprobs=[], tokens=[], top_logprobs=[])
    last_token_len = 0
    for token_id, id_logprob in zip(token_ids, id_logprobs):
        token = tokenizer.convert_ids_to_tokens(token_id)
        logprobs.tokens.append(token)
        logprobs.token_logprobs.append(id_logprob[token_id])
        if len(logprobs.text_offset) == 0:
            logprobs.text_offset.append(initial_text_offset)
        else:
            logprobs.text_offset.append(logprobs.text_offset[-1] + last_token_len)
        last_token_len = len(token)

        logprobs.top_logprobs.append(
            {
                tokenizer.convert_ids_to_tokens(i): p
                for i, p in id_logprob.items()
            }
        )
    return logprobs


async def generate_completion_stream_generator(
    result_generator, request: CompletionCreateParams, request_id: str, tokenizer,
) -> AsyncGenerator:
    previous_texts = [""] * request.n
    previous_num_tokens = [0] * request.n
    async for res in result_generator:
        res: RequestOutput
        for output in res.outputs:
            i = output.index
            output.text = output.text.replace("�", "")
            delta_text = output.text[len(previous_texts[i]):]

            if request.logprobs is not None:
                logprobs = create_logprobs(
                    tokenizer,
                    output.token_ids[previous_num_tokens[i]:],
                    output.logprobs[previous_num_tokens[i]:],
                    len(previous_texts[i]))
            else:
                logprobs = None

            previous_texts[i] = output.text
            previous_num_tokens[i] = len(output.token_ids)

            choice = CompletionChoice(
                index=i, text=delta_text, finish_reason="stop", logprobs=logprobs,
            )  # TODO: support for length
            chunk = Completion(
                id=request_id, choices=[choice], created=int(time.time()),
                model=request.model, object="text_completion",
            )
            yield chunk.model_dump_json()

            if output.finish_reason is not None:
                if request.logprobs is not None:
                    logprobs = Logprobs(text_offset=[], token_logprobs=[], tokens=[], top_logprobs=[])
                else:
                    logprobs = None
                choice = CompletionChoice(
                    index=i, text=delta_text, finish_reason="stop", logprobs=logprobs,
                )  # TODO: support for length
                chunk = Completion(
                    id=request_id, choices=[choice], created=int(time.time()),
                    model=request.model, object="text_completion",
                )
                yield chunk.model_dump_json(exclude_none=True)
