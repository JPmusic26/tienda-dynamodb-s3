# Tienda Digital Dynamo

Aplicación web en **Flask** desplegada en una instancia **EC2** que consulta y agrega registros en tablas de **DynamoDB** y guarda imágenes de productos en **S3**. Laboratorio #9 de Computación en la Nube (AWS).

## Qué hace

- Lista las tablas `clientes`, `productos` y `ventas` de DynamoDB.
- Agrega clientes (nombre, correo, ciudad) y productos (nombre, categoría, precio, foto opcional).
- Asigna el ID automáticamente (`C011`, `P012`...): toma el mayor existente y suma 1.
- Consulta un registro por ID directamente en DynamoDB (`get_item`).
- Sube las fotos de productos a S3 y las muestra con enlaces temporales (presigned URLs), sin hacer público el bucket.
- Lista los archivos del bucket en la pestaña "Archivos en S3".

## Arquitectura

```
Navegador ──HTTP:80──> EC2 (Flask) ──> DynamoDB (clientes, productos, ventas)
                                  └──> S3 (bucket privado)
```

La app **no usa llaves de acceso**: `boto3` toma las credenciales del rol IAM asociado a la instancia EC2.

## Estructura del proyecto

```
tienda-dynamodb-s3/
├── app.py              # Servidor Flask y API
├── index.html          # Página (se sirve desde Flask)
├── requirements.txt    # Dependencias de Python
├── tienda.service      # Servicio systemd (arranque automático)
├── iam-policy.json     # Política mínima para el rol de la EC2
└── README.md
```

## Requisitos en AWS

1. **Tablas en DynamoDB** (región `us-east-1`), con estos nombres en minúscula y clave de partición de tipo String:

   | Tabla | Clave de partición |
   |---|---|
   | `clientes` | `clienteId` |
   | `productos` | `productoId` |
   | `ventas` | `ventaId` |

2. **Bucket S3 privado** (por defecto `jorge-privado`; cámbialo por el tuyo).
3. **Rol IAM** para EC2 con la política de `iam-policy.json` (ajusta el nombre del bucket), asociado a la instancia en *Acciones → Seguridad → Modificar rol de IAM*.
4. **Grupo de seguridad** con regla de entrada HTTP (puerto 80).

## Despliegue en la EC2 (Amazon Linux 2023)

```bash
sudo dnf install -y python3-pip git
git clone https://github.com/TU-USUARIO/tienda-dynamodb-s3.git ~/tienda
cd ~/tienda
sudo pip3 install -r requirements.txt
```

Prueba manual (si Apache `httpd` ocupa el puerto 80, apágalo antes con `sudo systemctl disable --now httpd`):

```bash
sudo S3_BUCKET=TU-BUCKET python3 app.py
```

Abre `http://IP-PUBLICA-DE-LA-EC2`.

### Arranque automático con systemd

```bash
sudo cp tienda.service /etc/systemd/system/tienda.service
sudo nano /etc/systemd/system/tienda.service   # ajusta S3_BUCKET y la ruta si hace falta
sudo systemctl daemon-reload
sudo systemctl enable --now tienda
systemctl status tienda
```

## Variables de entorno

| Variable | Por defecto | Uso |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Región de DynamoDB y S3 |
| `S3_BUCKET` | `jorge-privado` | Bucket donde se guardan las fotos |

## API

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/clientes`, `/api/productos`, `/api/ventas` | Lista todos los registros |
| GET | `/api/consulta/<tabla>/<id>` | Consulta un registro por ID |
| GET | `/api/siguiente-id/<tabla>` | Próximo ID disponible (clientes, productos) |
| POST | `/api/clientes` | Agrega un cliente (JSON: nombre, correo, ciudad) |
| POST | `/api/productos` | Agrega un producto (form-data: nombre, categoria, precio, imagen) |
| GET | `/api/s3/archivos` | Lista las fotos del bucket |

## Notas

- Flask corre con su servidor de desarrollo, suficiente para el laboratorio. Para producción conviene usar gunicorn detrás de Nginx.
- Las consultas de listado usan `scan`, adecuado para tablas pequeñas.
- No subas llaves de acceso ni archivos `.pem` al repositorio (ya están en `.gitignore`).
