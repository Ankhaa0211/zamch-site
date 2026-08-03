"""
Seed demo store + products for local/dev marketplace preview.

Usage:
  python3 seed_demo.py

Idempotent: skips if demo seller phone already exists.
Demo login (seller): 99001100 / demo1234
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlmodel import Session, select

from main import (
    Category,
    Product,
    Store,
    User,
    Warehouse,
    create_db_and_tables,
    engine,
    hash_password,
    seed_categories,
)

DEMO_PHONE = "99001100"
DEMO_PASSWORD = "demo1234"
DEMO_STORE_NAME = "ЗАМЧ Demo Дугуй"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def seed() -> None:
    create_db_and_tables()
    seed_categories()

    with Session(engine) as session:
        cats = {c.slug: c for c in session.exec(select(Category)).all()}
        dugui = cats.get("dugui")
        obud = cats.get("obud")
        if not dugui or not obud:
            raise SystemExit("Categories missing — start the app once or run seed_categories")

        seller = session.exec(select(User).where(User.phone == DEMO_PHONE)).first()
        if not seller:
            seller = User(
                name="Demo Худалдагч",
                phone=DEMO_PHONE,
                password_hash=hash_password(DEMO_PASSWORD),
                role="seller",
            )
            session.add(seller)
            session.commit()
            session.refresh(seller)

        store = session.exec(select(Store).where(Store.owner_id == seller.id)).first()
        if not store:
            store = Store(
                owner_id=seller.id,
                name=DEMO_STORE_NAME,
                description="Туршилтын дэлгүүр — Приус, Land 200, RAV4-д түгээмэл хэмжээ.",
                phone=DEMO_PHONE,
                location="Улаанбаатар, Баянзүрх",
                logo="/static/logo.jpg",
                is_active=True,
                is_approved=True,
            )
            session.add(store)
            session.commit()
            session.refresh(store)
        else:
            # Ensure demo store is visible on the public web
            store.is_active = True
            store.is_approved = True
            store.logo = store.logo or "/static/logo.jpg"
            session.add(store)
            session.commit()

        warehouse = session.exec(
            select(Warehouse).where(
                Warehouse.store_id == store.id, Warehouse.is_default == True
            )
        ).first()
        if not warehouse:
            session.add(
                Warehouse(
                    store_id=store.id,
                    name="Үндсэн агуулах",
                    address="Улаанбаатар",
                    is_default=True,
                    is_active=True,
                )
            )
            session.commit()

        existing_products = session.exec(
            select(Product).where(Product.store_id == store.id)
        ).all()
        if existing_products:
            # Re-publish / restock so listings show on the public web
            for product in existing_products:
                product.is_active = True
                product.publish_status = "published"
                if int(product.stock or 0) < 1:
                    product.stock = 1
                if not product.published_at:
                    product.published_at = _now()
                session.add(product)
            session.commit()
            print(
                f"Demo already seeded (user=#{seller.id}, store=#{store.id}, products={len(existing_products)}) — refreshed visibility"
            )
            print(f"Seller login: {DEMO_PHONE} / {DEMO_PASSWORD}")
            return

        img = json.dumps(["/static/placeholder.jpg"])
        published = "published"
        now = _now()

        demos = [
            dict(
                category_id=dugui.id,
                listing_kind="dugui",
                title="Bridgestone 205/55 R16",
                brand="Bridgestone",
                condition="Шинэ",
                pack_type="4 хос",
                price=480000,
                stock=8,
                description="Prius / Camry-д түгээмэл зуны дугуй. Шинэ, баталгаатай.",
                width=205,
                ratio=55,
                diameter=16,
                tread_percent=100,
                car_make="Prius",
            ),
            dict(
                category_id=dugui.id,
                listing_kind="dugui",
                title="Yokohama 195/65 R15",
                brand="Yokohama",
                condition="Шинэ",
                pack_type="4 хос",
                price=360000,
                stock=12,
                description="Приус 20/30-д тохирох хэмжээ.",
                width=195,
                ratio=65,
                diameter=15,
                tread_percent=100,
                car_make="Prius",
            ),
            dict(
                category_id=dugui.id,
                listing_kind="dugui",
                title="Michelin 265/65 R17",
                brand="Michelin",
                condition="Шинэ",
                pack_type="4 хос",
                price=980000,
                stock=4,
                description="Prado / Hilux-д түгээмэл.",
                width=265,
                ratio=65,
                diameter=17,
                tread_percent=100,
                car_make="Prado",
            ),
            dict(
                category_id=dugui.id,
                listing_kind="dugui",
                title="Goodyear 285/60 R18",
                brand="Goodyear",
                condition="Хэрэглэж байсан",
                pack_type="4 хос",
                price=720000,
                stock=2,
                description="Land 200-д тохирох, протек ~70%.",
                width=285,
                ratio=60,
                diameter=18,
                tread_percent=70,
                car_make="Land 200",
            ),
            dict(
                category_id=obud.id,
                listing_kind="obud",
                title="Обуд R16 5x100",
                brand="Enkei",
                condition="Шинэ",
                pack_type="4 хос",
                price=550000,
                stock=3,
                description="Prius-д тохирох ойр нүхтэй обуд.",
                diameter=16,
                bolt_pattern="5x100",
                wheel_type="Хөнгөн цагаан",
                car_make="Prius",
            ),
            dict(
                category_id=obud.id,
                listing_kind="obud",
                title="Обуд R18 5x150",
                brand="OEM",
                condition="Хэрэглэж байсан",
                pack_type="4 хос",
                price=890000,
                stock=1,
                description="Land Cruiser 200-д тохирох нүх 5x150.",
                diameter=18,
                bolt_pattern="5x150",
                wheel_type="Хөнгөн цагаан",
                car_make="Land 200",
            ),
            dict(
                category_id=dugui.id,
                listing_kind="combo",
                title="Обудтай дугуй R17 225/65 5x114.3",
                brand="Toyota",
                condition="Шинэ",
                pack_type="4 хос",
                price=1250000,
                stock=2,
                description="RAV4-д бэлэн обудтай дугуй.",
                width=225,
                ratio=65,
                diameter=17,
                bolt_pattern="5x114.3",
                tread_percent=100,
                car_make="RAV4",
            ),
            dict(
                category_id=dugui.id,
                listing_kind="dugui",
                title="Hankook 235/55 R18",
                brand="Hankook",
                condition="Шинэ",
                pack_type="4 хос",
                price=620000,
                stock=6,
                description="Camry / RAV4-д түгээмэл.",
                width=235,
                ratio=55,
                diameter=18,
                tread_percent=100,
                car_make="Camry",
            ),
        ]

        for row in demos:
            session.add(
                Product(
                    store_id=store.id,
                    images=img,
                    publish_status=published,
                    published_at=now,
                    submitted_at=now,
                    is_active=True,
                    **row,
                )
            )

        session.commit()
        print(f"Seeded store #{store.id} «{store.name}» with {len(demos)} products")
        print(f"Seller login: {DEMO_PHONE} / {DEMO_PASSWORD}")


if __name__ == "__main__":
    seed()
