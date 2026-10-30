import os
import re
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

REGION = os.getenv("AWS_REGION", "us-east-1")
BUCKET = os.getenv("S3_BUCKET", "jorge-privado")
CARPETA = os.path.dirname(os.path.abspath(__file__))

# Sin llaves de acceso: boto3 usa el rol IAM asociado a la instancia EC2
dynamodb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)

# nombre en la URL -> (tabla en DynamoDB, clave de partición)
TABLAS = {
    "clientes": ("clientes", "clienteId"),
    "productos": ("productos", "productoId"),
    "ventas": ("ventas", "ventaId"),
}

PREFIJOS = {"clientes": "C", "productos": "P"}  # C001, C002... / P001, P002...

app = Flask(__name__)


def limpiar(x):
    """DynamoDB devuelve Decimal; lo pasamos a int/float para el JSON."""
    if isinstance(x, Decimal):
        return int(x) if x == x.to_integral_value() else float(x)
    if isinstance(x, list):
        return [limpiar(i) for i in x]
    if isinstance(x, dict):
        return {k: limpiar(v) for k, v in x.items()}
    return x


def escanear(nombre):
    tabla = dynamodb.Table(TABLAS[nombre][0])
    resp = tabla.scan()
    items = resp["Items"]
    while "LastEvaluatedKey" in resp:
        resp = tabla.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items += resp["Items"]
    return limpiar(items)


def siguiente_id(nombre):
    """Calcula el próximo ID: el mayor existente + 1 (C010 -> C011)."""
    clave = TABLAS[nombre][1]
    prefijo = PREFIJOS[nombre]
    numeros = []
    for i in escanear(nombre):
        m = re.fullmatch(prefijo + r"(\d+)", str(i.get(clave, "")))
        if m:
            numeros.append(int(m.group(1)))
    return f"{prefijo}{(max(numeros) if numeros else 0) + 1:03d}"


def con_foto(p):
    """Si el producto tiene imagen en S3, agrega un enlace temporal para verla."""
    if p.get("imagen"):
        p["imagenUrl"] = s3.generate_presigned_url(
            "get_object", Params={"Bucket": BUCKET, "Key": p["imagen"]}, ExpiresIn=3600
        )
    return p


def agregar(nombre, item):
    tabla, clave = TABLAS[nombre]
    # No deja sobrescribir un registro que ya existe
    dynamodb.Table(tabla).put_item(
        Item=item, ConditionExpression=f"attribute_not_exists({clave})"
    )


@app.errorhandler(ClientError)
def error_aws(e):
    if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
        return jsonify(error="Ese ID ya existe"), 409
    return jsonify(error=e.response["Error"]["Message"]), 500


@app.get("/")
def inicio():
    return send_from_directory(CARPETA, "index.html")


@app.get("/api/siguiente-id/<nombre>")
def ver_siguiente_id(nombre):
    if nombre not in PREFIJOS:
        return jsonify(error="Tabla no encontrada"), 404
    return jsonify(id=siguiente_id(nombre))


@app.get("/api/<nombre>")
def listar(nombre):
    if nombre not in TABLAS:
        return jsonify(error="Tabla no encontrada"), 404
    items = escanear(nombre)
    if nombre == "productos":
        items = [con_foto(p) for p in items]
    return jsonify(items)


@app.get("/api/consulta/<nombre>/<ident>")
def consultar(nombre, ident):
    """Consulta directa a DynamoDB (get_item) por la clave de partición."""
    if nombre not in TABLAS:
        return jsonify(error="Tabla no encontrada"), 404
    tabla, clave = TABLAS[nombre]
    ident = ident.strip()
    item = None
    for candidato in dict.fromkeys([ident, ident.upper()]):
        item = dynamodb.Table(tabla).get_item(Key={clave: candidato}).get("Item")
        if item:
            break
    if not item:
        return jsonify(error=f"No existe un registro con el ID {ident}"), 404
    item = limpiar(item)
    if nombre == "productos":
        item = con_foto(item)
    return jsonify(item)


@app.post("/api/clientes")
def nuevo_cliente():
    d = request.get_json(force=True)
    item = {k: str(d.get(k, "")).strip() for k in ("nombre", "correo", "ciudad")}
    if not all(item.values()):
        return jsonify(error="Completa todos los campos"), 400
    if "@" not in item["correo"]:
        return jsonify(error="Correo inválido"), 400
    item["clienteId"] = siguiente_id("clientes")
    agregar("clientes", item)
    return jsonify(item), 201


@app.post("/api/productos")
def nuevo_producto():
    f = request.form
    item = {k: f.get(k, "").strip() for k in ("nombre", "categoria")}
    if not all(item.values()):
        return jsonify(error="Completa todos los campos"), 400
    try:
        item["precio"] = Decimal(f.get("precio", "").strip())
    except Exception:
        return jsonify(error="Precio inválido"), 400

    item["productoId"] = siguiente_id("productos")

    foto = request.files.get("imagen")
    if foto and foto.filename:
        clave = f"productos/{item['productoId']}-{secure_filename(foto.filename)}"
        s3.upload_fileobj(foto, BUCKET, clave, ExtraArgs={"ContentType": foto.mimetype})
        item["imagen"] = clave

    agregar("productos", item)
    return jsonify(limpiar(item)), 201


@app.get("/api/s3/archivos")
def archivos_s3():
    r = s3.list_objects_v2(Bucket=BUCKET, Prefix="productos/")
    return jsonify([{"archivo": o["Key"], "bytes": o["Size"]} for o in r.get("Contents", [])])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
