"""Tests for four optional RAZOM coordination enhancements (demo-only data)."""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ['JWT_SECRET'] = 'unit-test-only-not-for-production-0123456789abcdef'
os.environ['DATABASE_URL'] = 'sqlite://'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
from razom_features import comparable, location, haversine_km


@pytest.fixture
def env():
    eng = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    main.engine = eng
    main.SessionLocal = sessionmaker(eng, expire_on_commit=False)
    main._guest_rates.clear()
    with TestClient(main.app) as client:
        with main.SessionLocal() as db:
            admin = main.User(full_name='Admin', email='admin@demo.invalid',
                              password_hash=main.password_hasher.hash('demo-long-password'),
                              role='admin', is_verified=True)
            applicant = main.User(full_name='Requester', email='applicant@demo.invalid',
                                  password_hash=main.password_hasher.hash('demo-long-password'),
                                  role='applicant', is_verified=True)
            vol = main.User(full_name='Volunteer', email='volunteer@demo.invalid',
                            password_hash=main.password_hasher.hash('demo-long-password'),
                            role='volunteer', is_verified=True)
            db.add_all([admin, applicant, vol]); db.commit()
            ids = (admin.id, applicant.id, vol.id)
        def auth(email):
            res = client.post('/auth/login', data={'username': email, 'password': 'demo-long-password'})
            assert res.status_code == 200
            return {'Authorization': 'Bearer ' + res.json()['access_token']}
        yield client, main.SessionLocal, ids, auth('admin@demo.invalid'), auth('volunteer@demo.invalid'), auth('applicant@demo.invalid')
    eng.dispose()


def create_req(db, uid, *, city='Львів', description='Потрібні продукти для родини з двома дітьми',
               category='food', created_at=None):
    r = main.HelpRequest(applicant_id=uid, city=city, description=description,
                         title='Потрібна допомога', address='Демо-адреса 1',
                         phone='+380000000000', category=category,
                         urgency=3, people_count=2, vulnerable_people=False,
                         status='new')
    if created_at:
        r.created_at = created_at
    db.add(r); db.flush()
    return r


def test_duplicate_detector_admin_only_and_privacy(env):
    c, sessions, ids, admin, vol, applicant = env
    with sessions() as db:
        first = create_req(db, ids[1])
        second = create_req(db, ids[1], description='Потрібні продукти для родини з двома дітьми!')
        create_req(db, ids[1], city='Чернівці')
        first_id, second_id = first.id, second.id
        db.commit()
    assert c.get('/admin/duplicates', headers=applicant).status_code == 403
    assert c.get('/admin/duplicates', headers=vol).status_code == 403
    response = c.get('/admin/duplicates', headers=admin)
    assert response.status_code == 200
    assert response.json()['matches'][0]['request_a'] == first_id
    assert response.json()['matches'][0]['request_b'] == second_id
    assert 'phone' not in response.text and '+380' not in response.text


def test_duplicate_detector_does_not_flag_different_descriptions():
    a = type('Req', (), {'id':1,'category':'food','city':'Львів',
                         'description':'Потрібні гарячі обіди на кожен день', 'phone':'000'})()
    b = type('Req', (), {'id':2,'category':'food','city':'Львів',
                         'description':'Допоможіть привезти візок для тварин', 'phone':'000'})()
    assert comparable(a,b) is None


def test_forecast_requires_history_and_then_works(env):
    c, sessions, ids, admin, _, _ = env
    with sessions() as db:
        create_req(db, ids[1]); db.commit()
    result = c.get('/admin/forecast', headers=admin)
    assert result.status_code == 200
    food = next(x for x in result.json()['categories'] if x['category'] == 'food')
    assert food['forecast_next_7d'] is None
    with sessions() as db:
        for i in range(15):
            when = datetime.now(timezone.utc) - timedelta(days=1 + i)
            create_req(db, ids[1], created_at=when)
        db.commit()
    result = c.get('/admin/forecast', headers=admin)
    food = next(x for x in result.json()['categories'] if x['category'] == 'food')
    assert food['enough_data'] is True
    assert isinstance(food['forecast_next_7d'], int)
    assert c.get('/admin/forecast').status_code == 401


def test_map_city_level_and_unknown_city(env):
    c, sessions, ids, admin, vol, _ = env
    with sessions() as db:
        create_req(db, ids[1], city='Тернопіль')
        create_req(db, ids[1], city='Невідоме-тестове-місто')
        db.commit()
    assert c.get('/admin/geography', headers=vol).status_code == 403
    resp = c.get('/admin/geography', headers=admin)
    assert resp.status_code == 200
    points = resp.json()['points']
    assert any(p['city'] == 'тернопіль' and p['requests'] == 1 for p in points)
    assert len(resp.json()['unmapped']) == 1
    assert 'address' not in resp.text and '+380' not in resp.text
    assert location('м. Тернопіль') is not None
    assert 400 < haversine_km(location('Львів'), location('Київ')) < 550


def test_smart_matching_local_and_logistics_remote_no_personal_data(env):
    c, sessions, ids, admin, vol, applicant = env
    with sessions() as db:
        local_req = create_req(db, ids[1], city='Львів')
        remote_req = create_req(db, ids[1], city='Тернопіль')
        private_guest = create_req(db, ids[1], city='Львів', description='Тестовий непідтверджений опис заявки')
        db.add(main.GuestReceipt(request_id=private_guest.id, token_sha256='f'*64))
        res = main.Resource(owner_id=ids[2], category='food', city='Львів', name='Пакунок', quantity=5, unit='шт.')
        db.add(res); db.commit()
        local_id, remote_id, private_id = local_req.id, remote_req.id, private_guest.id
    assert c.get('/smart-matches', headers=applicant).status_code == 403
    local = c.get('/smart-matches', headers=vol)
    assert local.status_code == 200
    assert [m['request_id'] for m in local.json()['matches']] == [local_id]
    logistics = c.get('/logistics/matches', headers=vol)
    assert logistics.status_code == 200
    matches = logistics.json()['matches']
    assert {m['request_id'] for m in matches} == {local_id, remote_id}
    assert private_id not in {m['request_id'] for m in matches}
    assert next(m for m in matches if m['request_id'] == local_id)['can_allocate_now'] is True
    remote = next(m for m in matches if m['request_id'] == remote_id)
    assert remote['can_allocate_now'] is False
    assert remote['distance_km_straight'] > 0
    assert 'phone' not in logistics.text and 'address' not in logistics.text
