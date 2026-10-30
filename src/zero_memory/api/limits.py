from fastapi.responses import JSONResponse


class RequestTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Bound streamed bodies too, including chunked multipart without Content-Length."""

    def __init__(self, app, upload_limit: int, restore_limit: int):
        self.app, self.upload_limit, self.restore_limit = app, upload_limit, restore_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        limit = (
            self.restore_limit
            if path == "/v1/restore"
            else self.upload_limit
            if path == "/v1/imports"
            else 2 * 1024 * 1024
        )
        total = 0
        started = False
        oversized = False
        error_sent = False
        error_response = JSONResponse(
            {"error": {"code": "upload_limit", "message": "请求体超过本机大小限制"}}, 413
        )

        async def limited_receive():
            nonlocal total, oversized
            message = await receive()
            total += len(message.get("body", b""))
            if total > limit:
                oversized = True
                raise RequestTooLarge()
            return message

        async def tracked_send(message):
            nonlocal started, error_sent
            # FastAPI converts body-reader exceptions into HTTP 400. Preserve our size error
            # even when a parser catches the exception, without buffering an unbounded body.
            if oversized:
                if message["type"] == "http.response.start":
                    started = True
                    await send(
                        {"type": "http.response.start", "status": 413, "headers": error_response.raw_headers}
                    )
                elif message["type"] == "http.response.body" and not error_sent:
                    error_sent = True
                    await send(
                        {"type": "http.response.body", "body": error_response.body, "more_body": False}
                    )
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestTooLarge:
            if started:
                raise
            await error_response(scope, receive, send)
