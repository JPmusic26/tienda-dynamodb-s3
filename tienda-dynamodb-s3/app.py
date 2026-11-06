import hmac
import os
import re
import secrets
import time
from datetime import timedelta
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf
from flask import Flask, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash
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
# Clave para firmar las sesiones. Defínela en el servicio (SECRET_KEY) para que las
# sesiones sobrevivan a un reinicio; si falta, se genera una nueva al arrancar.
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SEGURA") == "1",  # activar cuando haya HTTPS
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    WTF_CSRF_TIME_LIMIT=None,  # el token dura lo mismo que la sesión
)
csrf = CSRFProtect(app)  # exige el token CSRF en todas las peticiones POST/PUT/DELETE

USUARIOS = dynamodb.Table(os.getenv("TABLA_USUARIOS", "usuarios"))
# Si se define, quien quiera crear una cuenta debe escribir este código.
CODIGO_REGISTRO = os.getenv("CODIGO_REGISTRO", "")
# Hash de una contraseña aleatoria que nadie conoce: solo sirve para igualar el tiempo de respuesta
HASH_FALSO = generate_password_hash(secrets.token_urlsafe(32), method="pbkdf2:sha256")
INTENTOS = {}  # ip -> momentos de los intentos fallidos recientes
MAX_INTENTOS, VENTANA = 5, 300  # 5 fallos cada 5 minutos


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


_DUENO = {}


def dueno_bucket():
    """ID de la cuenta AWS dueña del bucket (env BUCKET_OWNER o la cuenta del rol actual)."""
    if "id" not in _DUENO:
        _DUENO["id"] = os.getenv("BUCKET_OWNER") or boto3.client(
            "sts", region_name=REGION
        ).get_caller_identity()["Account"]
    return _DUENO["id"]


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


def demasiados(llave):
    """True si esa llave (IP) ya superó los intentos permitidos en la ventana."""
    ahora = time.time()
    INTENTOS[llave] = [t for t in INTENTOS.get(llave, []) if ahora - t < VENTANA]
    return len(INTENTOS[llave]) >= MAX_INTENTOS


def anotar(llave):
    INTENTOS.setdefault(llave, []).append(time.time())


def abrir_sesion(fila):
    session.clear()
    session.permanent = True
    session["usuario"] = fila["usuario"]
    session["nombre"] = fila.get("nombre") or fila["usuario"]


def buscar_usuario(usuario):
    if not usuario:
        return None
    try:
        return USUARIOS.get_item(Key={"usuario": usuario}).get("Item")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None  # todavía no existe la tabla de usuarios
        raise


def crear_tabla_usuarios():
    try:
        dynamodb.create_table(
            TableName=USUARIOS.name,
            KeySchema=[{"AttributeName": "usuario", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "usuario", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise
    USUARIOS.wait_until_exists()


def guardar_usuario(fila):
    """Guarda el usuario sin pisar uno existente; crea la tabla si falta."""
    condicion = "attribute_not_exists(usuario)"
    try:
        USUARIOS.put_item(Item=fila, ConditionExpression=condicion)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        crear_tabla_usuarios()
        USUARIOS.put_item(Item=fila, ConditionExpression=condicion)


RUTAS_LIBRES = {"pagina_login", "iniciar_sesion", "config_registro", "registrarse", "token_csrf"}


@app.before_request
def exigir_sesion():
    if request.endpoint in RUTAS_LIBRES or "usuario" in session:
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="Tu sesión expiró. Inicia sesión de nuevo."), 401
    return redirect("/login")


@app.errorhandler(CSRFError)
def error_csrf(e):
    return jsonify(error="Token de seguridad inválido. Recarga la página."), 400


@app.get("/api/csrf")
def token_csrf():
    return jsonify(token=generate_csrf())


@app.get("/login")
def pagina_login():
    if "usuario" in session:
        return redirect("/")
    return send_from_directory(CARPETA, "login.html")


@app.post("/api/login")
def iniciar_sesion():
    ip = request.remote_addr or "?"
    if demasiados(ip):
        return jsonify(error="Demasiados intentos. Espera unos minutos."), 429

    d = request.get_json(silent=True) or {}
    usuario = str(d.get("usuario", "")).strip().lower()
    clave = str(d.get("clave", ""))
    fila = buscar_usuario(usuario)
    # Se calcula un hash exista o no el usuario, para que la demora sea la misma
    coincide = check_password_hash(fila["hash"] if fila else HASH_FALSO, clave)
    if not (fila and coincide):
        anotar(ip)
        return jsonify(error="Usuario o contraseña incorrectos"), 401

    INTENTOS.pop(ip, None)
    abrir_sesion(fila)
    return jsonify(ok=True)


@app.get("/api/registro/config")
def config_registro():
    return jsonify(codigo=bool(CODIGO_REGISTRO))


@app.post("/api/registro")
def registrarse():
    llave = "registro:" + (request.remote_addr or "?")
    if demasiados(llave):
        return jsonify(error="Demasiados intentos. Espera unos minutos."), 429
    anotar(llave)

    d = request.get_json(silent=True) or {}
    usuario = str(d.get("usuario", "")).strip().lower()
    nombre = str(d.get("nombre", "")).strip()[:60] or usuario
    clave = str(d.get("clave", ""))
    codigo = str(d.get("codigo", ""))

    if CODIGO_REGISTRO and not hmac.compare_digest(codigo.encode(), CODIGO_REGISTRO.encode()):
        return jsonify(error="El código de acceso no es correcto"), 403
    if not re.fullmatch(r"[a-z0-9._-]{3,30}", usuario):
        return jsonify(error="El usuario debe tener de 3 a 30 caracteres: letras, números, punto, guion o guion bajo"), 400
    if not 8 <= len(clave) <= 128:
        return jsonify(error="La contraseña debe tener entre 8 y 128 caracteres"), 400

    fila = {
        "usuario": usuario,
        "nombre": nombre,
        "hash": generate_password_hash(clave, method="pbkdf2:sha256"),
    }
    try:
        guardar_usuario(fila)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return jsonify(error="Ese usuario ya existe"), 409
        raise
    abrir_sesion(fila)
    return jsonify(ok=True), 201


@app.post("/api/logout")
def cerrar_sesion():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/yo")
def quien_soy():
    return jsonify(usuario=session["usuario"], nombre=session["nombre"])


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
    r = s3.list_objects_v2(
        Bucket=BUCKET, Prefix="productos/", ExpectedBucketOwner=dueno_bucket()
    )
    return jsonify([{"archivo": o["Key"], "bytes": o["Size"]} for o in r.get("Contents", [])])


if __name__ == "__main__":
    # Por defecto solo escucha en local; en el servidor define HOST en el servicio systemd
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "80")))
