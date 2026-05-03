"""Build a lowlevel MCP Server that forwards every request to an upstream ClientSession."""
from __future__ import annotations

from mcp import ClientSession, types
from mcp.server.lowlevel import Server


def build_proxy_server(
    name: str,
    upstream: ClientSession,
    upstream_caps: types.ServerCapabilities,
) -> Server:
    proxy: Server = Server(name)

    if upstream_caps.tools is not None:
        async def _list_tools(req: types.ListToolsRequest):
            result = await upstream.list_tools(cursor=req.params.cursor if req.params else None)
            return types.ServerResult(result)

        async def _call_tool(req: types.CallToolRequest):
            params = req.params
            result = await upstream.call_tool(params.name, params.arguments or None)
            return types.ServerResult(result)

        proxy.request_handlers[types.ListToolsRequest] = _list_tools
        proxy.request_handlers[types.CallToolRequest] = _call_tool

    if upstream_caps.resources is not None:
        async def _list_resources(req: types.ListResourcesRequest):
            result = await upstream.list_resources(cursor=req.params.cursor if req.params else None)
            return types.ServerResult(result)

        async def _list_resource_templates(req: types.ListResourceTemplatesRequest):
            result = await upstream.list_resource_templates(
                cursor=req.params.cursor if req.params else None
            )
            return types.ServerResult(result)

        async def _read_resource(req: types.ReadResourceRequest):
            result = await upstream.read_resource(req.params.uri)
            return types.ServerResult(result)

        proxy.request_handlers[types.ListResourcesRequest] = _list_resources
        proxy.request_handlers[types.ListResourceTemplatesRequest] = _list_resource_templates
        proxy.request_handlers[types.ReadResourceRequest] = _read_resource

        if upstream_caps.resources.subscribe:
            async def _subscribe(req: types.SubscribeRequest):
                await upstream.subscribe_resource(req.params.uri)
                return types.ServerResult(types.EmptyResult())

            async def _unsubscribe(req: types.UnsubscribeRequest):
                await upstream.unsubscribe_resource(req.params.uri)
                return types.ServerResult(types.EmptyResult())

            proxy.request_handlers[types.SubscribeRequest] = _subscribe
            proxy.request_handlers[types.UnsubscribeRequest] = _unsubscribe

    if upstream_caps.prompts is not None:
        async def _list_prompts(req: types.ListPromptsRequest):
            result = await upstream.list_prompts(cursor=req.params.cursor if req.params else None)
            return types.ServerResult(result)

        async def _get_prompt(req: types.GetPromptRequest):
            params = req.params
            result = await upstream.get_prompt(params.name, params.arguments or None)
            return types.ServerResult(result)

        proxy.request_handlers[types.ListPromptsRequest] = _list_prompts
        proxy.request_handlers[types.GetPromptRequest] = _get_prompt

    if upstream_caps.logging is not None:
        async def _set_level(req: types.SetLevelRequest):
            await upstream.set_logging_level(req.params.level)
            return types.ServerResult(types.EmptyResult())

        proxy.request_handlers[types.SetLevelRequest] = _set_level

    if upstream_caps.completions is not None:
        async def _complete(req: types.CompleteRequest):
            params = req.params
            argument = {params.argument.name: params.argument.value}
            context_args = params.context.arguments if params.context else None
            result = await upstream.complete(params.ref, argument, context_args)
            return types.ServerResult(result)

        proxy.request_handlers[types.CompleteRequest] = _complete

    return proxy
