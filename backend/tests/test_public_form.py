import os

os.environ["APP_DB_URL"] = "sqlite:///:memory:"
os.environ["APP_ALLOW_SQLITE"] = "1"

from fastapi.testclient import TestClient

from main import app


def test_token_and_response_flow():
    with TestClient(app) as client:
        login = client.post("/auth/login", json={"email": "admin@local", "password": "admin123"})
        assert login.status_code == 200
        token = login.json()["access_token"]

        headers = {"Authorization": f"Bearer {token}"}

        suppliers = client.get("/suppliers", headers=headers).json()
        products = client.get("/products", headers=headers).json()
        campaigns = client.get("/campaigns", headers=headers).json()

        assert suppliers
        assert products
        assert campaigns

        campaign_id = campaigns[0]["id"]
        supplier_id = suppliers[0]["id"]

        items_payload = {"items": [{"product_id": products[0]["id"], "requested_qty": 10}]}
        resp_items = client.post(f"/campaigns/{campaign_id}/items", headers=headers, json=items_payload)
        assert resp_items.status_code == 200

        rec_payload = {"group_ids": [], "supplier_ids": [supplier_id]}
        rec = client.post(f"/campaigns/{campaign_id}/recipients", headers=headers, json=rec_payload)
        assert rec.status_code == 200
        token_value = rec.json()[0]["token"]

        form_get = client.get(f"/r/{token_value}")
        assert form_get.status_code == 200

        form_post = client.post(
            f"/r/{token_value}",
            data={
                "email": "supplier@example.com",
                f"qty_{products[0]['id']}": "5",
                f"price_{products[0]['id']}": "12.5",
                f"conform_{products[0]['id']}": "yes",
            },
        )
        assert form_post.status_code == 200

        responses = client.get(f"/campaigns/{campaign_id}/responses", headers=headers)
        assert responses.status_code == 200
        assert len(responses.json()) >= 1
