"""Crea (o actualiza) un usuario en la tabla 'usuarios' de DynamoDB.

Uso, desde la carpeta de la app en la EC2:
    sudo python3 crear_usuario.py

Se ejecuta con sudo para que boto3 use el rol IAM de la instancia.
La contraseña se guarda con hash (PBKDF2), nunca en texto plano.
"""
import getpass
import os
import sys

import boto3
from botocore.exceptions import ClientError
from werkzeug.security import generate_password_hash

REGION = os.getenv("AWS_REGION", "us-east-1")
NOMBRE_TABLA = os.getenv("TABLA_USUARIOS", "usuarios")

db = boto3.resource("dynamodb", region_name=REGION)


def asegurar_tabla():
    try:
        db.meta.client.describe_table(TableName=NOMBRE_TABLA)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        print(f"Creando la tabla '{NOMBRE_TABLA}'...")
        db.create_table(
            TableName=NOMBRE_TABLA,
            KeySchema=[{"AttributeName": "usuario", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "usuario", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        db.Table(NOMBRE_TABLA).wait_until_exists()
    return db.Table(NOMBRE_TABLA)


def main():
    tabla = asegurar_tabla()

    usuario = input("Usuario: ").strip().lower()
    if not usuario:
        sys.exit("El usuario no puede estar vacío.")
    nombre = input("Nombre para mostrar (Enter = igual al usuario): ").strip() or usuario

    clave = getpass.getpass("Contraseña (mínimo 8 caracteres): ")
    if len(clave) < 8:
        sys.exit("La contraseña es muy corta.")
    if clave != getpass.getpass("Repite la contraseña: "):
        sys.exit("Las contraseñas no coinciden.")

    tabla.put_item(
        Item={
            "usuario": usuario,
            "nombre": nombre,
            "hash": generate_password_hash(clave, method="pbkdf2:sha256"),
        }
    )
    print(f"Listo: el usuario '{usuario}' ya puede iniciar sesión.")


if __name__ == "__main__":
    main()
