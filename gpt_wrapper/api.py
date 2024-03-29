'''
Interactions with the OpenAI API and wrappers around the API.
'''

from typing import AsyncIterator

from openai import OpenAI, AsyncOpenAI, AsyncStream
from openai.resources.chat.completions import NOT_GIVEN
# API return values
from openai.types.chat.chat_completion import (
    ChatCompletion, # Overall Completion, has id, stats, choices
    ChatCompletionMessage, # completion.choice[0].message, has role, content, tool_calls
)

import openai.types.chat.chat_completion as Normal
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall as ToolCall,
    Function
)

from .utils import mock_response, mock_streaming_response, MockResponse


async def openai_chat(**openai_kwargs):
    '''
    Thin wrapper around openai with mocking and potential for other features/backend
    '''
    if openai_kwargs['model'] == 'mock':
        if openai_kwargs.get('stream'):
            return await mock_streaming_response("Hello, world!")
        else:
            return mock_response("Hello, world!")
    elif openai_kwargs['model'] == 'echo':
        last_msg = openai_kwargs['messages'][-1]['content']
        if openai_kwargs.get('stream'):
            return await mock_streaming_response(last_msg)
        else:
            return mock_response(last_msg)
        
    # new client for each call (TODO: is this efficient?)
    client = AsyncOpenAI()
    return await client.chat.completions.create(**openai_kwargs)


async def accumulate_partial(stream: AsyncStream[ChatCompletionChunk], track_usage=False) -> AsyncIterator[tuple[ChatCompletionChunk, Normal.ChatCompletion]]:
    '''
    Adapter that accumulates a stream of deltas into a stream of partial messages,
    e.g. "I", "love", "sushi" -> "I", "I love", "I love sushi"
    Almost everything will be indistinguishable from a normal completion, except:
    - `completion.usage` will be None, unless `track_usage` is set and prompt is provided.
    - `completion.choices[].finish_reason` will be 'length' during the partials.
    - `completion.choices[].message.tool_calls[].id` will be an empty string during the partials.
        -> HOWEVER, experiments show that tool ids and names are always included in the initial delta, so this should never be actually visible.
    '''
    completion = None

    try:
        async for chunk in stream:
            if completion is None:
                completion = Normal.ChatCompletion(
                    id=chunk.id,
                    choices=[], # populated later
                    created=chunk.created,
                    model=chunk.model,
                    object="chat.completion",
                    system_fingerprint=chunk.system_fingerprint,
                    usage=None # TODO: populate this by counting tokens
                )

            for delta_choice in chunk.choices:
                # ensure this choice exists in the completion
                if len(completion.choices) <= delta_choice.index:
                    completion.choices.extend([
                        Normal.Choice(
                            finish_reason='length', # NOTE: this is a fallback
                            index=i,
                            logprobs=None,
                            message=Normal.ChatCompletionMessage(role='assistant', content=None)
                        )
                        for i in range(len(completion.choices), delta_choice.index + 1)
                    ])
                
                choice = completion.choices[delta_choice.index]
                message = choice.message
                delta_message = delta_choice.delta

                # update the choice
                choice.finish_reason = delta_choice.finish_reason or choice.finish_reason

                # update the message
                message.role = delta_message.role or message.role
                
                if delta_message.content:
                    if not message.content:
                        message.content = delta_message.content
                    else:
                        message.content += delta_message.content
                    # TODO: update usage stats

                if delta_message.tool_calls:
                    # ensure tool_calls list exists
                    if message.tool_calls is None:
                        message.tool_calls = [] # populated later

                    for delta_tool_call in delta_message.tool_calls:
                        # ensure this tool_call exists in the message
                        if len(message.tool_calls) <= delta_tool_call.index:
                            message.tool_calls.extend([
                                ToolCall(
                                    id='', # NOTE: no initial id
                                    type='function',
                                    function=Function(name='', arguments='')
                                )
                                for i in range(len(message.tool_calls), delta_tool_call.index + 1)
                            ])
                        
                        tool_call = message.tool_calls[delta_tool_call.index]

                        # update the tool_call
                        tool_call.id = delta_tool_call.id or tool_call.id
                        tool_call.type = delta_tool_call.type or tool_call.type

                        # update the function
                        delta_function = delta_tool_call.function
                        if delta_function:
                            # experimental testing shows that the function name is always fully returned, no matter how long
                            tool_call.function.name = delta_function.name or tool_call.function.name

                            if delta_function.arguments:
                                tool_call.function.arguments += delta_function.arguments

            yield chunk, completion
    except Exception as e:
        print("Error in accumulate_partial:", e)
        raise e
    finally:
        await stream.response.aclose()