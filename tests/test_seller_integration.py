from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

import main


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_seller_product_order_reservation_and_release(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
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
                "phone": "69990001",
                "password": "secret1",
                "store_name": "Test Store",
                "location": "Улаанбаатар",
            },
        )
        assert seller.status_code == 200
        seller_token = seller.json()["token"]
        seller_headers = _auth(seller_token)

        warehouses = client.get(
            "/api/mobile/warehouses", headers=seller_headers
        ).json()["data"]
        category = client.get("/api/categories").json()["data"][0]
        product_response = client.post(
            "/api/mobile/products",
            headers=seller_headers,
            data={
                "category_id": category["id"],
                "title": "Тест дугуй",
                "price": "150000",
                "description": "Стандарт хангасан дэлгэрэнгүй барааны тайлбар.",
                "initial_stock": "3",
                "warehouse_id": warehouses[0]["id"],
            },
        )
        assert product_response.status_code == 200
        product_id = product_response.json()["id"]

        with Session(engine) as session:
            store = session.get(main.Store, seller.json()["store"]["id"])
            store.is_approved = True
            product = session.get(main.Product, product_id)
            product.publish_status = "published"
            session.add(store)
            session.add(product)
            session.commit()

        buyer = client.post(
            "/api/auth/register",
            data={"name": "Buyer", "phone": "69990002", "password": "secret1"},
        )
        assert buyer.status_code == 200
        assert client.post(
            "/api/cart", data={"product_id": product_id, "quantity": 2}
        ).status_code == 200
        checkout = client.post(
            "/api/orders",
            data={
                "customer_name": "Buyer",
                "customer_phone": "69990002",
                "delivery_type": "pickup",
                "payment_method": "cod",
            },
        )
        assert checkout.status_code == 200, checkout.text
        order_id = checkout.json()["order_ids"][0]

        with Session(engine) as session:
            order = session.get(main.Order, order_id)
            product = session.get(main.Product, product_id)
            balance = session.exec(
                select(main.InventoryBalance).where(
                    main.InventoryBalance.product_id == product_id
                )
            ).one()
            assert order.inventory_status == "reserved"
            assert product.stock == 1
            assert balance.quantity == 1

        confirm = client.patch(
            f"/api/mobile/orders/{order_id}/status",
            headers=seller_headers,
            data={"status": "confirmed"},
        )
        assert confirm.status_code == 200
        assert confirm.json()["inventory_status"] == "committed"

        cancel = client.patch(
            f"/api/mobile/orders/{order_id}/status",
            headers=seller_headers,
            data={"status": "cancelled", "cancellation_reason": "Бараа гэмтэлтэй"},
        )
        assert cancel.status_code == 200
        assert cancel.json()["inventory_status"] == "released"

        with Session(engine) as session:
            product = session.get(main.Product, product_id)
            balance = session.exec(
                select(main.InventoryBalance).where(
                    main.InventoryBalance.product_id == product_id
                )
            ).one()
            assert product.stock == 3
            assert balance.quantity == 3


def test_pending_order_expires_and_releases_stock(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'expiry.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(main, "engine", engine)
    main.SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        user = main.User(
            name="Seller",
            phone="69990003",
            password_hash=main.hash_password("secret1"),
            role="seller",
        )
        session.add(user)
        session.flush()
        store = main.Store(
            owner_id=user.id,
            name="Expiry Store",
            phone=user.phone,
            is_approved=True,
        )
        session.add(store)
        session.flush()
        category = main.Category(name="Дугуй", slug="dugui")
        session.add(category)
        session.flush()
        warehouse = main.Warehouse(
            store_id=store.id, name="Үндсэн", is_default=True
        )
        session.add(warehouse)
        session.flush()
        product = main.Product(
            store_id=store.id,
            category_id=category.id,
            title="Expiry product",
            price=100,
            stock=0,
            publish_status="published",
        )
        session.add(product)
        session.flush()
        order = main.Order(
            store_id=store.id,
            customer_name="Buyer",
            customer_phone="69990004",
            status="pending",
            inventory_status="reserved",
            confirmation_expires_at=(
                datetime.now() - timedelta(minutes=1)
            ).isoformat(timespec="seconds"),
        )
        session.add(order)
        session.flush()
        session.add(
            main.OrderItem(
                order_id=order.id,
                product_id=product.id,
                title=product.title,
                price=product.price,
                quantity=1,
            )
        )
        session.commit()
        assert main._expire_pending_orders(session) == 1
        session.refresh(order)
        session.refresh(product)
        assert order.status == "cancelled"
        assert order.inventory_status == "released"
        assert product.stock == 1
