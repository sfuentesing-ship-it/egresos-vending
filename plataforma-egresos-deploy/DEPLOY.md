# Desplegar en línea (Render.com, gratis)

Esta guía sube la plataforma a internet con una URL fija que puedes compartir.

## 1. Subir el código a GitHub

1. Crea una cuenta gratis en https://github.com si no tienes.
2. Entra a https://github.com/new y crea un repositorio:
   - Nombre: `egresos-vending`
   - Visibilidad: **Private**
   - Clic en "Create repository".
3. En la página del repositorio, clic en "uploading an existing file".
4. Descomprime `plataforma-egresos-deploy.zip` y arrastra TODOS los archivos (excepto `.venv`, `cache.json` y `config.json` que no van).
5. Clic en "Commit changes".

## 2. Crear el servicio en Render

1. Crea una cuenta gratis en https://render.com (puedes entrar con tu cuenta de GitHub).
2. Clic en **New +** → **Web Service**.
3. Conecta tu repositorio `egresos-vending`.
4. Configura:
   - **Name:** `egresos-vending`
   - **Runtime:** Python
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python app.py`
   - **Instance Type:** Free
5. En **Environment Variables**, agrega:

| Variable | Valor |
|---|---|
| `TENANT` | `vendingstore` |
| `PARROTFY_COOKIE` | (la cookie completa, ver abajo) |
| `PARROTFY_CSRF` | (el token csrf) |
| `APP_USER` | (usuario para entrar a la plataforma, tú lo eliges) |
| `APP_PASSWORD` | (contraseña para entrar, tú la eliges) |

6. Clic en **Create Web Service** y espera ~3 minutos.
7. Tu plataforma quedará en: `https://egresos-vending.onrender.com`

## 3. Obtener la cookie y csrf de Parrotfy

1. Entra a https://vendingstore.parrotfy.com/inventory_movements
2. Presiona `F12` → pestaña **Network** → `F5`
3. Clic en la petición `inventory_movements.json` → **Headers → Request Headers**
4. Copia el valor de `cookie:` y de `x-csrf-token:` y pégalos en las variables de Render.

> ⚠️ La sesión de Parrotfy caduca cada cierto tiempo (días). Cuando la plataforma muestre "Sesión expirada", repite este paso y actualiza las variables en Render (Settings → Environment).

## Notas

- El plan gratis de Render "duerme" el servicio tras 15 minutos sin uso. La primera visita del día puede tardar ~1 minuto en cargar; después funciona normal.
- Cada egreso se guarda para siempre, así que las actualizaciones son rápidas.
- La plataforma se actualiza sola cada 4 horas, o al presionar "Consultar".
