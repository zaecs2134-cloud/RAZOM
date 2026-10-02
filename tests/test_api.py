"""Integration tests: accounts, roles, privacy, matching, atomic updates, analytics."""
import os

os.environ["JWT_SECRET"] = "unit-test-only-not-for-production-0123456789abcdef"
os.environ["DATABASE_URL"] = "sqlite://"

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main


@pytest.fixture
def setup():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    main._guest_rates.clear()
    main.engine = eng
    main.SessionLocal = sessionmaker(eng, expire_on_commit=False)
    with TestClient(main.app) as client:
        yield client, main.SessionLocal
    eng.dispose()


def register(client, email, role):
    resp = client.post("/auth/register", json={"email": email, "full_name": "Example User",
                                                      "password": "very-long-demo-password", "role": role})
    assert resp.status_code == 201, resp.text
    return resp.json()


def token(client, email):
    resp = client.post("/auth/login", data={"username": email,
                                             "password": "very-long-demo-password"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": "Bearer " + resp.json()["access_token"]}


def make_request(client, headers, category="food", city="Тернопіль"):
    return client.post("/requests", headers=headers, json={
        "title": "Допомога продуктами", "description": "Тестова заявка без реальних даних",
        "city": city, "address": "Вигадана вулиця 12", "phone": "+380000000000",
        "category": category, "urgency": 4, "people_count": 3,
        "vulnerable_people": True,
    })


def test_complete_flow_and_privacy(setup):
    c, sessionmaker_ = setup
    applicant = register(c, "applicant@example.com", "applicant")
    stranger = register(c, "stranger@example.com", "applicant")
    volunteer = register(c, "volunteer@example.com", "volunteer")
    volunteer2 = register(c, "volunteer2@example.com", "volunteer")
    assert volunteer["is_verified"] is False
    ah, sh = token(c, applicant["email"]), token(c, stranger["email"])
    vh, v2h = token(c, volunteer["email"]), token(c, volunteer2["email"])

    assert c.get("/requests").status_code == 401
    assert c.post("/resources", headers=ah, json={
        "name": "Pack", "category": "food", "city": "Тернопіль", "quantity": 2,
        "unit": "шт.",
    }).status_code == 403

    resp = make_request(c, ah)
    assert resp.status_code == 201, resp.text
    item_id = resp.json()["id"]
    assert resp.json()["address"] == "Вигадана вулиця 12"
    assert c.get(f"/requests/{item_id}", headers=sh).status_code == 403
    assert c.get(f"/requests/{item_id}", headers=vh).status_code == 403
    public = c.get("/requests", headers=vh)
    assert public.status_code == 200
    assert len(public.json()) == 1
    assert "address" not in public.json()[0]
    assert "phone" not in public.json()[0]
    assert "description" not in public.json()[0]
    assert c.post(f"/requests/{item_id}/claim", headers=vh).status_code == 403
    assert c.get("/suggestions", headers=vh).status_code == 403

    with sessionmaker_() as db:
        db.add(main.User(full_name="Admin", email="admin@example.com", role="admin",
                         is_verified=True, password_hash=main.password_hasher.hash(
                             "very-long-demo-password")))
        db.commit()
    adminh = token(c, "admin@example.com")
    assert len(c.get("/admin/volunteers", headers=adminh).json()) == 2
    assert c.patch(f"/admin/users/{volunteer['id']}/verify", headers=vh).status_code == 403
    assert c.patch(f"/admin/users/{volunteer['id']}/verify", headers=adminh).status_code == 200
    assert c.patch(f"/admin/users/{volunteer2['id']}/verify", headers=adminh).status_code == 200

    res = c.post("/resources", headers=vh, json={
        "name": "Тестовий набір", "category": "food", "city": "Тернопіль",
        "quantity": 3, "unit": "пак.",
    })
    assert res.status_code == 201, res.text
    resource_id = res.json()["id"]
    matches = c.get("/suggestions", headers=vh)
    assert matches.status_code == 200
    assert [x["request"]["id"] for x in matches.json()] == [item_id]
    assert matches.json()[0]["compatible_resources"][0]["id"] == resource_id
    assert c.get("/suggestions", headers=v2h).json() == []

    claim = c.post(f"/requests/{item_id}/claim", headers=vh)
    assert claim.status_code == 200, claim.text
    assert claim.json()["assigned_volunteer_id"] == volunteer["id"]
    assert c.post(f"/requests/{item_id}/claim", headers=v2h).status_code == 409
    assert c.get(f"/requests/{item_id}", headers=vh).status_code == 200
    assert c.get(f"/requests/{item_id}", headers=v2h).status_code == 403

    assert c.post(f"/resources/{resource_id}/allocate", headers=v2h,
                  json={"request_id": item_id, "quantity": 1}).status_code == 403
    alloc = c.post(f"/resources/{resource_id}/allocate", headers=vh,
                   json={"request_id": item_id, "quantity": 2})
    assert alloc.status_code == 201, alloc.text
    assert c.get("/resources", headers=vh).json()[0]["quantity"] == 1
    assert c.post(f"/resources/{resource_id}/allocate", headers=vh,
                  json={"request_id": item_id, "quantity": 2}).status_code == 409
    assert c.get("/resources", headers=vh).json()[0]["quantity"] == 1
    assert c.post(f"/requests/{item_id}/complete", headers=vh).status_code == 200
    assert c.post(f"/requests/{item_id}/complete", headers=vh).status_code == 409
    stats = c.get("/admin/analytics", headers=adminh)
    assert stats.status_code == 200
    assert stats.json()["requests_total"] == 1
    assert stats.json()["allocated_units"] == 2
    assert stats.json()["verified_volunteers"] == 2
    assert stats.json()["by_status"]["completed"] == 1
    assert c.get("/admin/analytics", headers=vh).status_code == 403


def test_score_and_self_registration(setup):
    c, _ = setup
    resp = c.post("/auth/register", json={"full_name": "Evil Admin",
        "email": "fakeadmin@example.com", "password": "a-long-test-password", "role": "admin"})
    assert resp.status_code == 422
    register(c, "one@example.com", "applicant")
    assert c.post("/auth/register", json={"full_name": "Duplicate",
        "email": "ONE@example.com", "password": "a-long-test-password", "role": "applicant"}).status_code == 409
    hdr = token(c, "one@example.com")
    low = make_request(c, hdr)
    high = c.post("/requests", headers=hdr, json={"title":"Евакуаційний запит",
        "description":"Вигаданий тестовий запит на евакуацію", "city":"Тернопіль",
        "address":"Вигаданий проспект 14", "phone":"+380000000000",
        "category":"evacuation", "urgency":5, "people_count":4,
        "vulnerable_people": True})
    assert high.status_code == 201
    assert high.json()["priority_score"] > low.json()["priority_score"]
    assert c.get("/requests", headers=hdr).json()[0]["id"] == high.json()["id"]
    assert c.post(f"/requests/{low.json()['id']}/cancel", headers=hdr).status_code == 200
    assert c.post(f"/requests/{low.json()['id']}/cancel", headers=hdr).status_code == 409


def test_frontend_and_health(setup):
    c, _ = setup
    home = c.get("/")
    assert home.status_code == 200
    assert "разом" in home.text
    assert "Створити заявку" in home.text
    assert c.get("/health").json() == {"status": "ok"}
    assert c.get("/openapi.json").status_code == 200


def test_guest_submission_status_and_admin_review(setup):
    c, sessions = setup
    # A visitor can submit without authentication but cannot see private details.
    payload = {"category":"food", "city":"Львів",
        "description":"Вигаданий запит на продукти для тестування",
        "phone":"+380000000000", "people_count":4, "urgency":3,
        "vulnerable_people":False, "consent":True}
    resp = c.post('/public/requests', json=payload)
    assert resp.status_code == 201, resp.text
    item_id = resp.json()['request_id']
    secret = resp.json()['tracking_code']
    assert len(secret) >= 32
    assert c.get(f'/requests/{item_id}').status_code == 401
    assert c.post('/public/requests/status', json={
        'request_id':item_id, 'tracking_code':'a'*43}).status_code == 404
    status = c.post('/public/requests/status', json={
        'request_id':item_id, 'tracking_code':secret})
    assert status.status_code == 200
    assert status.json()['needs_review'] is True
    assert 'phone' not in status.json() and 'description' not in status.json()
    volunteer = register(c, 'newvol@example.com', 'volunteer')
    vheader = token(c, volunteer['email'])
    with sessions() as db:
        db.add(main.User(full_name='Admin',email='guestadmin@example.com',role='admin',
                is_verified=True,password_hash=main.password_hasher.hash('very-long-demo-password')))
        db.commit()
    aheader = token(c, 'guestadmin@example.com')
    assert c.patch(f'/admin/users/{volunteer["id"]}/verify', headers=aheader).status_code == 200
    assert c.get('/requests',headers=vheader).json() == []
    assert c.post(f'/requests/{item_id}/claim',headers=vheader).status_code == 409
    pending = c.get('/admin/guest-requests/pending',headers=aheader)
    assert pending.status_code == 200
    assert [r['id'] for r in pending.json()] == [item_id]
    assert c.get('/admin/guest-requests/pending',headers=vheader).status_code == 403
    assert c.patch(f'/admin/guest-requests/{item_id}/review',headers=aheader).status_code == 200
    assert c.patch(f'/admin/guest-requests/{item_id}/review',headers=aheader).status_code == 409
    assert [r['id'] for r in c.get('/requests',headers=vheader).json()] == [item_id]
    assert c.post(f'/requests/{item_id}/claim',headers=vheader).status_code == 200
    updated = c.post('/public/requests/status',json={'request_id':item_id,'tracking_code':secret})
    assert updated.status_code == 200
    assert updated.json()['needs_review'] is False
    assert updated.json()['status'] == 'assigned'


def test_guest_form_validation_honeypot_and_rate_limit(setup):
    c, _ = setup
    body = {"category":"other", "city":"Київ", "description":"Вигадане звернення для тестування",
            "phone":"+380000000000", "people_count":2, "consent":True}
    assert c.post('/public/requests',json={**body, 'consent':False}).status_code == 422
    assert c.post('/public/requests',json={**body, 'website':'hidden-bot'}).status_code == 422
    # Test client has one shared IP; in-memory limits are deliberately not production-grade.
    for _ in range(4):
        assert c.post('/public/requests',json=body).status_code == 201
    assert c.post('/public/requests',json=body).status_code == 429
