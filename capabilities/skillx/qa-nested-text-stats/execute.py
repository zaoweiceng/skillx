async def execute(request, context):
    return await context.capabilities.call('stats', request.input)
