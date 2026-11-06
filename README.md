# Tienda Digital Dynamo

Aplicación web en **Flask** desplegada en una instancia **EC2** que consulta y agrega registros en tablas de **DynamoDB** y guarda imágenes de productos en **S3**, con inicio de sesión. Laboratorio #9 de Computación en la Nube (AWS).

## Qué hace

- Pide iniciar sesión antes de mostrar nada (usuarios guardados en DynamoDB, contraseñas con hash).
- Permite crear una cuenta desde la misma pantalla de login (con código de acceso opcional).
- Lista las tablas `clientes`, `productos` y `ventas`.
- Agrega clientes (nombre, correo, ciudad) y productos (nombre, categoría, precio, foto opcional).
- Asigna el ID automáticamente (`C011`, `P012`...): toma el mayor existente y suma 1.
- Consulta un registro por ID directamente en DynamoDB (`get_item`).
- Sube las fotos de productos a S3 y las muestra con enlaces temporales (presigned URLs), sin hacer público el bucket.
- Lista los archivos del bucket en la pestaña "Archivos en S3".

## Arquitectura

```
Navegador ──HTTP:80──> EC2 (Flask) ──> DynamoDB (clientes, productos, ventas, usuarios)
                                  └──> S3 (bucket privado)
```

La app **no usa llaves de acceso**: `boto3` toma las credenciales del rol IAM asociado a la instancia EC2.

## Estructura del proyecto

```
tienda-dynamodb-s3/
├── app.py              # Servidor Flask, API y sesiones
├── index.html          # Panel (requiere sesión)
├── login.html          # Pantalla de inicio de sesión
├── crear_usuario.py    # Crea usuarios en DynamoDB
├── requirements.txt    # Dependencias de Python
├── tienda.service      # Servicio systemd (arranque automático)
├── iam-policy.json     # Política mínima para el rol de la EC2
└── README.md
```

## Requisitos en AWS

1. **Tablas en DynamoDB** (región `us-east-1`), con clave de partición de tipo String:

   | Tabla | Clave de partición |
   |---|---|
   | `clientes` | `clienteId` |
   | `productos` | `productoId` |
   | `ventas` | `ventaId` |
   | `usuarios` | `usuario` (se crea sola al registrar la primera cuenta, o con `crear_usuario.py`) |

2. **Bucket S3 privado** (por defecto `jorge-privado`; cámbialo por el tuyo).
3. **Rol IAM** para EC2 con la política de `iam-policy.json` (ajusta el nombre del bucket), asociado a la instancia en *Acciones → Seguridad → Modificar rol de IAM*. Para crear la tabla `usuarios` con el script, el rol necesita además `dynamodb:CreateTable` y `dynamodb:DescribeTable` (o `AmazonDynamoDBFullAccess` en un entorno de laboratorio).
4. **Grupo de seguridad** con regla de entrada HTTP (puerto 80).

## Despliegue en la EC2 (Amazon Linux 2023)

```bash
sudo dnf install -y python3-pip git
git clone https://github.com/TU-USUARIO/tienda-dynamodb-s3.git ~/tienda
cd ~/tienda
sudo pip3 install -r requirements.txt
```

### Crear el primer usuario

La forma más fácil es abrir la página y usar **Crear cuenta** en la pantalla de login. También puedes crearlo desde la terminal; se ejecuta con `sudo` para que use el rol de la instancia:

```bash
sudo python3 crear_usuario.py
```

Pide usuario, nombre para mostrar y contraseña (mínimo 8 caracteres). Para cambiar una contraseña, vuelve a ejecutarlo con el mismo usuario.

### Prueba manual

Si Apache (`httpd`) ocupa el puerto 80, apágalo antes con `sudo systemctl disable --now httpd`.

```bash
sudo S3_BUCKET=TU-BUCKET python3 app.py
```

Abre `http://IP-PUBLICA-DE-LA-EC2`.

### Arranque automático con systemd

Genera una clave para firmar las sesiones y pégala en `SECRET_KEY` dentro de `tienda.service`:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

```bash
sudo cp tienda.service /etc/systemd/system/tienda.service
sudo nano /etc/systemd/system/tienda.service   # ajusta S3_BUCKET y SECRET_KEY
sudo systemctl daemon-reload
sudo systemctl enable --now tienda
systemctl status tienda
```

## Variables de entorno

| Variable | Por defecto | Uso |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Región de DynamoDB y S3 |
| `S3_BUCKET` | `jorge-privado` | Bucket donde se guardan las fotos |
| `SECRET_KEY` | aleatoria al arrancar | Firma de las sesiones; defínela para que no se cierren al reiniciar |
| `TABLA_USUARIOS` | `usuarios` | Nombre de la tabla de usuarios |
| `CODIGO_REGISTRO` | vacío (registro abierto) | Si se define, quien cree una cuenta debe escribir este código |
| `COOKIE_SEGURA` | desactivada | Ponla en `1` cuando la app se sirva por HTTPS |

## API

Todas las rutas `/api/*` (salvo `/api/login`, `/api/registro` y `/api/registro/config`) responden `401` si no hay sesión.

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/api/login` | Inicia sesión (JSON: usuario, clave) |
| POST | `/api/registro` | Crea una cuenta e inicia sesión (JSON: nombre, usuario, clave, codigo) |
| GET | `/api/registro/config` | Indica si el registro pide código de acceso |
| POST | `/api/logout` | Cierra la sesión |
| GET | `/api/yo` | Usuario de la sesión actual |
| GET | `/api/clientes`, `/api/productos`, `/api/ventas` | Lista todos los registros |
| GET | `/api/consulta/<tabla>/<id>` | Consulta un registro por ID |
| GET | `/api/siguiente-id/<tabla>` | Próximo ID disponible (clientes, productos) |
| POST | `/api/clientes` | Agrega un cliente (JSON: nombre, correo, ciudad) |
| POST | `/api/productos` | Agrega un producto (form-data: nombre, categoria, precio, imagen) |
| GET | `/api/s3/archivos` | Lista las fotos del bucket |

## Seguridad

- Las contraseñas se guardan con hash PBKDF2; nunca en texto plano.
- Tras 5 intentos fallidos desde una misma IP, el login se bloquea 5 minutos.
- **Cualquiera que llegue a la página puede intentar crear una cuenta.** Define `CODIGO_REGISTRO` para que solo entre quien conozca el código; sin él, el registro queda abierto.
- El registro también limita los intentos (5 cada 5 minutos por IP) y pide contraseñas de 8 a 128 caracteres.
- La sesión dura 8 horas y la cookie es `HttpOnly` y `SameSite=Lax`.
- **El sitio se sirve por HTTP**, así que usuario y contraseña viajan sin cifrar. Es aceptable en un laboratorio, pero para un uso real hay que poner HTTPS (por ejemplo con CloudFront o un balanceador con certificado de ACM, o Nginx con Let's Encrypt) y activar `COOKIE_SEGURA=1`.
- Flask corre con su servidor de desarrollo; en producción conviene usar gunicorn detrás de Nginx.
- No subas llaves de acceso ni archivos `.pem` al repositorio (ya están en `.gitignore`).
