import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ["BUCKET_OWNER"] = "123456789012"

import pytest
from werkzeug.security import generate_password_hash

import app as tienda


@pytest.fixture
def cliente():
    tienda.app.config["TESTING"] = True
    tienda.INTENTOS.clear()
    with tienda.app.test_client() as c:
        yield c


def token(c):
    return c.get("/api/csrf").get_json()["token"]


def test_post_sin_token_csrf_es_rechazado(cliente):
    r = cliente.post("/api/login", json={"usuario": "a", "clave": "b"})
    assert r.status_code == 400


def test_login_correcto(cliente):
    fila = {"usuario": "ana", "nombre": "Ana", "hash": generate_password_hash("clave1234")}
    with patch.object(tienda, "buscar_usuario", return_value=fila):
        r = cliente.post("/api/login", json={"usuario": "ana", "clave": "clave1234"},
                         headers={"X-CSRFToken": token(cliente)})
    assert r.status_code == 200
    assert cliente.get("/api/yo").get_json()["nombre"] == "Ana"


def test_login_incorrecto(cliente):
    with patch.object(tienda, "buscar_usuario", return_value=None):
        r = cliente.post("/api/login", json={"usuario": "x", "clave": "y"},
                         headers={"X-CSRFToken": token(cliente)})
    assert r.status_code == 401


def test_api_sin_sesion_da_401(cliente):
    assert cliente.get("/api/clientes").status_code == 401


def test_listar_s3_usa_expected_bucket_owner(cliente):
    with cliente.session_transaction() as s:
        s["usuario"], s["nombre"] = "ana", "Ana"
    falso = MagicMock()
    falso.list_objects_v2.return_value = {"Contents": [{"Key": "productos/a.png", "Size": 5}]}
    with patch.object(tienda, "s3", falso):
        r = cliente.get("/api/s3/archivos")
    assert r.get_json() == [{"archivo": "productos/a.png", "bytes": 5}]
    assert falso.list_objects_v2.call_args.kwargs["ExpectedBucketOwner"] == "123456789012"


def test_limpiar_decimal():
    from decimal import Decimal
    assert tienda.limpiar({"a": [Decimal("2"), Decimal("2.5")]}) == {"a": [2, 2.5]}


def iniciar(c):
    with c.session_transaction() as s:
        s["usuario"], s["nombre"] = "ana", "Ana"


def h(c):
    return {"X-CSRFToken": token(c)}


def tabla_falsa(items=None, get=None):
    t = MagicMock()
    t.scan.return_value = {"Items": items or []}
    t.get_item.return_value = {"Item": get} if get else {}
    db = MagicMock()
    db.Table.return_value = t
    return db, t


def test_registro_valida_usuario_y_clave(cliente):
    r = cliente.post("/api/registro", json={"usuario": "!!", "clave": "12345678"}, headers=h(cliente))
    assert r.status_code == 400
    r = cliente.post("/api/registro", json={"usuario": "ana", "clave": "123"}, headers=h(cliente))
    assert r.status_code == 400


def test_registro_ok_y_duplicado(cliente):
    with patch.object(tienda, "guardar_usuario"):
        r = cliente.post("/api/registro", json={"usuario": "ana", "clave": "12345678"}, headers=h(cliente))
    assert r.status_code == 201
    err = tienda.ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem")
    with patch.object(tienda, "guardar_usuario", side_effect=err):
        r = cliente.post("/api/registro", json={"usuario": "ana", "clave": "12345678"}, headers=h(cliente))
    assert r.status_code == 409


def test_registro_con_codigo(cliente):
    with patch.object(tienda, "CODIGO_REGISTRO", "secreto"):
        r = cliente.post("/api/registro", json={"usuario": "ana", "clave": "12345678", "codigo": "mal"},
                         headers=h(cliente))
        assert r.status_code == 403
        assert cliente.get("/api/registro/config").get_json() == {"codigo": True}


def test_bloqueo_por_demasiados_intentos(cliente):
    for _ in range(tienda.MAX_INTENTOS):
        tienda.anotar("1.2.3.4")
    assert tienda.demasiados("1.2.3.4")


def test_logout(cliente):
    iniciar(cliente)
    assert cliente.post("/api/logout", headers=h(cliente)).status_code == 200
    assert cliente.get("/api/yo").status_code == 401


def test_paginas(cliente):
    assert cliente.get("/login").status_code == 200
    assert cliente.get("/").status_code == 302
    iniciar(cliente)
    assert cliente.get("/").status_code == 200
    assert cliente.get("/login").status_code == 302


def test_listar_y_siguiente_id(cliente):
    iniciar(cliente)
    db, _ = tabla_falsa([{"clienteId": "C009", "nombre": "A"}])
    with patch.object(tienda, "dynamodb", db):
        assert cliente.get("/api/clientes").get_json()[0]["nombre"] == "A"
        assert cliente.get("/api/siguiente-id/clientes").get_json() == {"id": "C010"}
    assert cliente.get("/api/nada").status_code == 404
    assert cliente.get("/api/siguiente-id/ventas").status_code == 404


def test_listar_productos_con_foto(cliente):
    iniciar(cliente)
    db, _ = tabla_falsa([{"productoId": "P001", "imagen": "productos/a.png"}])
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "http://url"
    with patch.object(tienda, "dynamodb", db), patch.object(tienda, "s3", s3):
        r = cliente.get("/api/productos").get_json()
    assert r[0]["imagenUrl"] == "http://url"


def test_consultar(cliente):
    iniciar(cliente)
    db, _ = tabla_falsa(get={"clienteId": "C001"})
    with patch.object(tienda, "dynamodb", db):
        assert cliente.get("/api/consulta/clientes/c001").status_code == 200
    db, _ = tabla_falsa()
    with patch.object(tienda, "dynamodb", db):
        assert cliente.get("/api/consulta/clientes/C999").status_code == 404
    assert cliente.get("/api/consulta/otra/C1").status_code == 404


def test_nuevo_cliente(cliente):
    iniciar(cliente)
    db, t = tabla_falsa()
    ok = {"nombre": "A", "correo": "a@b.co", "ciudad": "X"}
    with patch.object(tienda, "dynamodb", db):
        r = cliente.post("/api/clientes", json=ok, headers=h(cliente))
        assert r.status_code == 201 and r.get_json()["clienteId"] == "C001"
        assert cliente.post("/api/clientes", json={**ok, "correo": "x"}, headers=h(cliente)).status_code == 400
        assert cliente.post("/api/clientes", json={}, headers=h(cliente)).status_code == 400


def test_nuevo_producto(cliente):
    iniciar(cliente)
    db, _ = tabla_falsa()
    s3 = MagicMock()
    with patch.object(tienda, "dynamodb", db), patch.object(tienda, "s3", s3):
        malo = cliente.post("/api/productos", data={"nombre": "a", "categoria": "b", "precio": "x"}, headers=h(cliente))
        assert malo.status_code == 400
        vacio = cliente.post("/api/productos", data={"nombre": "", "categoria": "b"}, headers=h(cliente))
        assert vacio.status_code == 400
        import io
        r = cliente.post("/api/productos", headers=h(cliente), content_type="multipart/form-data",
                         data={"nombre": "a", "categoria": "b", "precio": "9.5",
                               "imagen": (io.BytesIO(b"x"), "foto.png")})
    assert r.status_code == 201
    s3.upload_fileobj.assert_called_once()


def test_error_aws_duplicado(cliente):
    iniciar(cliente)
    err = tienda.ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem")
    db, t = tabla_falsa()
    t.put_item.side_effect = err
    with patch.object(tienda, "dynamodb", db):
        r = cliente.post("/api/clientes", json={"nombre": "A", "correo": "a@b.co", "ciudad": "X"}, headers=h(cliente))
    assert r.status_code == 409


def test_buscar_y_guardar_usuario():
    assert tienda.buscar_usuario("") is None
    no_existe = tienda.ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "GetItem")
    tabla = MagicMock()
    tabla.get_item.side_effect = no_existe
    with patch.object(tienda, "USUARIOS", tabla):
        assert tienda.buscar_usuario("ana") is None
    tabla = MagicMock()
    tabla.put_item.side_effect = [no_existe, None]
    with patch.object(tienda, "USUARIOS", tabla), patch.object(tienda, "dynamodb", MagicMock()):
        tienda.guardar_usuario({"usuario": "ana"})
    assert tabla.put_item.call_count == 2
