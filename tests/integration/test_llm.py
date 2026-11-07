import httpx
import pytest

from evocode.config import AppConfig
from evocode.llm import ModelClient


async def test_schema_output_request_cannot_silently_fall_back(monkeypatch):
    from evocode.llm import ModelError
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"code": "response_format_not_supported"}})

    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: actual(transport=httpx.MockTransport(handler), **kw))
    config = AppConfig()
    config.models.local.model_id = "provider-test"
    with pytest.raises(ModelError, match="does not support"):
        await ModelClient(config).complete(
            [{"role": "user", "content": "Return JSON"}],
            response_format={"type": "json_schema", "json_schema": {"name": "test", "schema": {}}},
        )
    assert len(requests) == 1


@pytest.mark.parametrize('thinking',[None,'disabled','enabled'])
async def test_provider_thinking_option_and_reasoning_history_are_preserved(monkeypatch,thinking):
    import json
    requests=[]
    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200,json={'model':'provider-test','choices':[{'message':{
            'content':'{"action":"respond","answer":"ok"}',
            'reasoning_content':'provider continuation metadata',
        }}],'usage':{'total_tokens':12}})
    actual=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:actual(transport=httpx.MockTransport(handler),**kw))
    cfg=AppConfig();cfg.models.local.model_id='provider-test';cfg.models.local.thinking=thinking
    history=[{'role':'assistant','content':'prior','reasoning_content':'prior provider metadata'},
             {'role':'user','content':'continue'}]
    response=await ModelClient(cfg).complete(history)
    if thinking is None:
        assert 'thinking' not in requests[0]
    else:
        assert requests[0]['thinking']=={'type':thinking}
    assert requests[0]['messages'][0]['reasoning_content']=='prior provider metadata'
    assert response.reasoning_content=='provider continuation metadata'
    assert response.usage['total_tokens']==12


async def test_provider_response_diagnostics_are_preserved(monkeypatch):
    import json

    raw_message = {
        'content': 'answer',
        'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {
            'name': 'lookup', 'arguments': '{"query":"sample"}',
        }}],
    }
    def handler(req):
        return httpx.Response(200, headers={'x-request-id': 'req-header'}, json={
            'id': 'resp-body', 'model': 'provider-test',
            'choices': [{'message': raw_message, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 8, 'completion_tokens': 3, 'total_tokens': 11},
        })
    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(handler), **kw))
    cfg = AppConfig(); cfg.models.local.model_id = 'provider-test'

    response = await ModelClient(cfg).complete([{'role': 'user', 'content': 'goal'}])

    assert response.finish_reason == 'tool_calls'
    assert response.response_id == 'resp-body'
    assert response.raw_message == raw_message
    assert response.usage == {'prompt_tokens': 8, 'completion_tokens': 3, 'total_tokens': 11}
    assert json.loads(response.model_dump_json())['raw_message'] == raw_message


async def test_invalid_tool_arguments_keep_provider_usage_and_raw_diagnostics(monkeypatch):
    import json

    from evocode.llm import ModelError

    raw_message = {'content': None, 'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {
        'name': 'lookup', 'arguments': '{invalid json',
    }}]}
    def handler(req):
        return httpx.Response(200, headers={'x-request-id': 'req-header'}, json={
            'id': 'resp-123', 'model': 'provider-test',
            'choices': [{'message': raw_message, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 17, 'completion_tokens': 5, 'total_tokens': 22},
        })
    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(handler), **kw))
    cfg = AppConfig(); cfg.models.local.model_id = 'provider-test'

    with pytest.raises(ModelError) as captured:
        await ModelClient(cfg).complete([{'role': 'user', 'content': 'goal'}])

    error = captured.value
    assert error.usage == {'prompt_tokens': 17, 'completion_tokens': 5, 'total_tokens': 22}
    assert error.metadata == {
        'model': 'provider-test', 'response_id': 'resp-123',
        'finish_reason': 'tool_calls', 'raw_message': raw_message,
    }
    assert error.raw_output == raw_message
    assert json.loads(json.dumps(error.metadata))['raw_message']['tool_calls'][0]['function']['arguments'] == '{invalid json'

async def test_structured_output_fallback_merges_system_without_changing_tools(monkeypatch):
    requests=[]
    def handler(req):
        import json
        body=json.loads(req.content);requests.append(body)
        if 'response_format' in body:
            return httpx.Response(400,json={'error':{'code':'response_format_not_supported'}})
        return httpx.Response(200,json={'model':'test','choices':[{'message':{'content':'{"ready":true}'}}]})
    actual=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:actual(transport=httpx.MockTransport(handler),**kw))
    c=AppConfig();c.models.local.model_id='test'
    result=await ModelClient(c).complete([{'role':'system','content':'rules'},{'role':'user','content':'goal'}],response_format={'type':'json_object'})
    assert result.content=='{"ready":true}'
    assert len(requests)==2
    assert [m['role'] for m in requests[-1]['messages']]==['system','user']
    assert 'rules' in requests[-1]['messages'][0]['content']


async def test_exact_token_count_requires_explicit_exact_response_and_counts_full_request(monkeypatch):
    import json
    requests=[]
    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json={'input_tokens': 321, 'exact': True})
    actual=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:actual(transport=httpx.MockTransport(handler),**kw))
    cfg=AppConfig(); cfg.models.local.model_id='provider-test'; cfg.models.local.token_count_url='http://tokenizer/count'
    messages=[{'role':'system','content':'rules'},{'role':'user','content':'goal'}]
    tools=[{'type':'function','function':{'name':'lookup','parameters':{'type':'object'}}}]
    result=await ModelClient(cfg).count_input_tokens(
        messages, role='planner', tools=tools,
        response_format={'type':'json_object'}, max_tokens=2048,
    )
    assert result == 321
    assert requests[0] == {
        'model':'provider-test', 'messages':messages, 'tools':tools,
        'response_format':{'type':'json_object'}, 'max_tokens':2048,
    }


async def test_token_count_api_does_not_claim_exact_without_exact_flag(monkeypatch):
    from evocode.llm import ModelError
    def handler(req):
        return httpx.Response(200, json={'input_tokens': 321})
    actual=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:actual(transport=httpx.MockTransport(handler),**kw))
    cfg=AppConfig(); cfg.models.local.model_id='provider-test'; cfg.models.local.token_count_url='http://tokenizer/count'
    with pytest.raises(ModelError, match='exact=true'):
        await ModelClient(cfg).count_input_tokens([{'role':'user','content':'goal'}])


async def test_token_counter_merges_systems_and_never_sends_model_key_cross_origin(monkeypatch):
    import json
    seen=[]
    def handler(req):
        seen.append((dict(req.headers), json.loads(req.content)))
        return httpx.Response(200, json={'input_tokens': 10, 'exact': True})
    actual=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:actual(transport=httpx.MockTransport(handler),**kw))
    monkeypatch.setenv('EVOCODE_LOCAL_API_KEY','test-secret')
    cfg=AppConfig(); cfg.models.local.model_id='provider-test'
    cfg.models.local.base_url='https://api.example/v1'
    cfg.models.local.token_count_url='https://counter.example/count'
    cfg.models.local.thinking='disabled'
    await ModelClient(cfg).count_input_tokens([
        {'role':'system','content':'one'}, {'role':'system','content':'two'}, {'role':'user','content':'goal'}
    ])
    headers, body=seen[0]
    assert 'authorization' not in headers
    assert body['messages'][0] == {'role':'system','content':'one\n\ntwo'}
    assert body['thinking'] == {'type':'disabled'}


def test_model_context_and_output_reserves_must_leave_positive_input_space():
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match='max_output_tokens must be smaller'):
        AppConfig.model_validate({'models': {'local': {
            'max_context_tokens': 2048,
            'max_output_tokens': 2048,
        }}})


async def test_structured_output_retry_recounts_mutated_payload_and_rejects_overflow(monkeypatch):
    import json

    from evocode.llm import ModelError
    requests=[]
    def handler(req):
        body=json.loads(req.content)
        requests.append((req.url.path, body))
        if req.url.path.endswith('/chat/completions'):
            if 'response_format' in body:
                return httpx.Response(400, json={'error': {'code': 'response_format_not_supported'}})
            return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})
        return httpx.Response(200, json={'input_tokens': 900, 'exact': True})
    actual=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:actual(transport=httpx.MockTransport(handler),**kw))
    cfg=AppConfig(); cfg.models.local.model_id='provider-test'
    cfg.models.local.base_url='https://api.example/v1'
    cfg.models.local.token_count_url='https://api.example/token-count'
    cfg.models.local.max_context_tokens=1024
    cfg.models.local.max_output_tokens=100
    cfg.runtime.context_safety_margin_tokens=100
    with pytest.raises(ModelError, match='structured-output fallback input'):
        await ModelClient(cfg).complete(
            [{'role':'system','content':'rules'},{'role':'user','content':'goal'}],
            response_format={'type':'json_object'}, max_tokens=100,
        )
    count_request=next(body for path, body in requests if path.endswith('/token-count'))
    assert 'response_format' not in count_request
    assert count_request['messages'][0]['content'].endswith('Return a single valid JSON object without markdown fences.')
