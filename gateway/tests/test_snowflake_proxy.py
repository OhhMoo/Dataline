import httpx

from alkera_gateway.snowflake_proxy import proxy_app


async def test_wire_routes_forward_body_and_reject_rest():
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    app = proxy_app("http://fake", httpx.MockTransport(upstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tls"
    ) as c:
        response = await c.post("/session/v1/login-request?requestId=1", content=b'{"data":{}}')
        assert response.json()["success"]
        assert requests[0].content == b'{"data":{}}'
        assert requests[0].url.params["requestId"] == "1"
        assert (await c.post("/api/v2/statements")).status_code == 404
        assert len(requests) == 1


async def test_upstream_failure_is_a_bounded_wire_error():
    app = proxy_app("http://fake", httpx.MockTransport(lambda _: httpx.Response(500)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tls"
    ) as c:
        response = await c.post("/queries/v1/query-request")
        assert response.status_code == 200
        assert not response.json()["success"]
