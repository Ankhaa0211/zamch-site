from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

import main


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_seller_feedback_submit_and_admin_list(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'feedback.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "IS_SQLITE", True)
    monkeypatch.setattr(main, "AUTO_CREATE_SCHEMA", True)

    with TestClient(main.app) as client:
        seller = client.post(
            "/api/mobile/auth/register",
            data={
                "name": "Seller",
                "phone": "69990011",
                "password": "secret1",
                "store_name": "Feedback Store",
                "location": "Улаанбаатар",
            },
        )
        assert seller.status_code == 200
        token = seller.json()["token"]
        store_id = seller.json()["store"]["id"]

        too_short = client.post(
            "/api/mobile/feedback",
            headers=_auth(token),
            data={"message": "hi"},
        )
        assert too_short.status_code == 400

        unauth = client.post("/api/mobile/feedback", data={"message": "enough text here"})
        assert unauth.status_code == 401

        ok = client.post(
            "/api/mobile/feedback",
            headers=_auth(token),
            data={"message": "Зарсан бүртгэх дээр алдаа гарлаа"},
        )
        assert ok.status_code == 200
        assert ok.json()["ok"] is True

        with Session(engine) as session:
            rows = session.exec(select(main.SellerFeedback)).all()
            assert len(rows) == 1
            assert rows[0].store_id == store_id
            assert rows[0].status == "new"
            assert "Зарсан" in rows[0].message

        # Admin session via cookie login
        admin = client.post(
            "/api/auth/register",
            data={"name": "Admin", "phone": "69990012", "password": "secret1"},
        )
        assert admin.status_code == 200
        with Session(engine) as session:
            user = session.exec(
                select(main.User).where(main.User.phone == "69990012")
            ).first()
            user.role = "admin"
            session.add(user)
            session.commit()

        login = client.post(
            "/api/auth/login",
            data={"phone": "69990012", "password": "secret1"},
        )
        assert login.status_code == 200

        listed = client.get("/api/admin/feedback")
        assert listed.status_code == 200
        data = listed.json()["data"]
        assert len(data) == 1
        assert data[0]["message"].startswith("Зарсан")
        assert data[0]["status"] == "new"

        marked = client.post(f"/api/admin/feedback/{data[0]['id']}/read")
        assert marked.status_code == 200
        assert marked.json()["status"] == "read"
