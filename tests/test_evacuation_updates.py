"""RAZOM 2.6: test data only; never use real contact information."""
import os
import sys
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


@pytest.fixture
def client_db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    main.engine = engine
    main.SessionLocal = sessionmaker(engine, expire_on_commit=False)
    main._guest_rates.clear()
    with TestClient(main.app) as client:
        with main.SessionLocal() as db:
            admin = main.User(full_name='Admin', email='admin@demo.invalid',
                              password_hash=main.password_hasher.hash('demo-long-password'),
                              role='admin', is_verified=True)
            volunteer = main.User(full_name='Volunteer', email='volunteer@demo.invalid',
                                  password_hash=main.password_hasher.hash('demo-long-password'),
                                  role='volunteer', is_verified=True)
            db.add_all([admin, volunteer]); db.commit()
            admin_id = admin.id
        def auth(email):
            response = client.post('/auth/login', data={'username': email, 'password': 'demo-long-password'})
            assert response.status_code == 200
            return {'Authorization': 'Bearer ' + response.json()['access_token']}
        yield client, main.SessionLocal, admin_id, auth('admin@demo.invalid'), auth('volunteer@demo.invalid')
    engine.dispose()


def payload(**kwargs):
    return {'category':'evacuation', 'city':'Чернігів', 'description':'Тестова евакуаційна заявка без реальних даних',
            'phone':'+380000000000', 'people_count':2, 'consent':True, **kwargs}


def test_guest_address_stored_privately_and_optional(client_db):
    client, sessions, _, admin, volunteer = client_db
    p = payload()
    p['address'] = 'Вигадана вулиця, 12'
    created = client.post('/public/requests', json=p)
    assert created.status_code == 201, created.text
    request_id = created.json()['request_id']
    assert 'address' not in created.text
    status = client.post('/public/requests/status', json={
        'request_id': request_id, 'tracking_code': created.json()['tracking_code']})
    assert status.status_code == 200
    assert 'address' not in status.text
    assert client.get(f'/requests/{request_id}', headers=volunteer).status_code == 403
    private = client.get(f'/requests/{request_id}', headers=admin)
    assert private.status_code == 200
    assert private.json()['address'] == 'Вигадана вулиця, 12'
    p2 = payload()
    p2['city'] = 'Ніжин'
    p2.pop('address', None)
    created2 = client.post('/public/requests', json=p2)
    assert created2.status_code == 201
    assert client.get(f'/requests/{created2.json()["request_id"]}', headers=admin).json()['address'] == 'Уточнюється після зв\'язку'


def test_guest_address_max_length(client_db):
    client, *_ = client_db
    p = payload()
    p['address'] = 'А' * 251
    assert client.post('/public/requests', json=p).status_code == 422


def test_evacuation_pins_and_admin_only(client_db):
    client, sessions, admin_id, admin, volunteer = client_db
    with sessions() as db:
        for city, category, status in [
            ('Чернігів', 'evacuation', 'new'),
            ('Чернігів', 'food', 'new'),
            ('Чернігів', 'evacuation', 'assigned'),
            ('Тестове невідоме село', 'evacuation', 'new'),
            ('Чернігів', 'evacuation', 'completed'),
        ]:
            db.add(main.HelpRequest(
                applicant_id=admin_id, title='Тестова заявка', description='Вигадана ситуація для тесту',
                city=city, address='Дуже приватна тестова адреса', phone='+380000000000',
                category=category, urgency=3, people_count=1, vulnerable_people=False,
                status=status))
        db.commit()
    assert client.get('/admin/geography', headers=volunteer).status_code == 403
    response = client.get('/admin/geography', headers=admin)
    assert response.status_code == 200
    body = response.json()
    known = next(p for p in body['points'] if p['city'] == 'чернігів')
    assert known['requests'] == 3
    assert known['evacuation_requests'] == 2
    assert len(known['evacuation_request_ids']) == 2
    missing = next(p for p in body['unmapped'] if p['city'] == 'тестове невідоме село')
    assert missing['evacuation_requests'] == 1
    assert 'Дуже приватна' not in response.text and '+380' not in response.text
    assert 'address' not in response.text and 'phone' not in response.text


def test_form_and_map_filter_present(client_db):
    client, *_ = client_db
    response = client.get('/')
    assert response.status_code == 200
    assert 'id="haddress"' in response.text
    assert 'id="mapCategory"' in response.text
    assert 'razomGeocodeSettlement' in response.text
    assert 'data-map-request' in response.text
