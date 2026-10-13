import pytest


@pytest.mark.parametrize(
    "path", ["/", "/orders/5b0e7a4e-0f0e-4d3e-9a55-0d6a9b5b5b5b", "/orders/not-a-uuid"]
)
async def test_every_page_route_serves_the_timeline_page(client, path):
    response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for element in ('id="submit-form"', 'id="timeline"', 'id="not-found"'):
        assert element in response.text


async def test_the_page_routes_do_not_shadow_the_api(client):
    response = await client.get("/api/orders/not-a-uuid")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


async def test_the_page_is_revalidated_so_a_new_deploy_is_seen_at_once(client):
    response = await client.get("/")

    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"]
