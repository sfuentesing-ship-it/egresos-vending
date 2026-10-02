# Plataforma de Egresos - Vending Store

Dashboard que muestra los egresos de bodega (retiros de reponedores) registrados en Parrotfy, sumados por reponedor.

## Funcionamiento

- Los datos se cargan desde Parrofy y se guardan en caché durante 4 horas.
- El dashboard se actualiza solo cada 4 horas (o al presionar **Consultar**).
- Los egresos se agrupan y suman por reponedor (columna "# Documento" / "Notas" de Parrotfy).

## Requisitos

- Windows con Python 3.12 (ya instalado en este PC)
- El entorno virtual ya está creado en `.venv`

## Iniciar

Doble clic en `iniciar.bat`, o desde PowerShell:

```powershell
cd "C:\Users\Vending Store\Documents\Proyecto predeterminado\plataforma-egresos"
.\.venv\Scripts\python.exe app.py
```

Luego abre en el navegador:

- En este PC: http://127.0.0.1:5000
- Desde otro dispositivo de la misma red: http://192.168.1.206:5000

## Actualizar la sesión de Parrotfy

La plataforma usa tu sesión del navegador (cookie). Cuando la sesión expire verás el aviso "Sesión de Parrotfy expirada". Para renovarla:

1. Entra a https://vendingstore.parrotfy.com/inventory_movements
2. Presiona `F12` → pestaña **Network** → `F5`
3. Haz clic en la petición `inventory_movements.json`
4. En **Headers → Request Headers** copia:
   - `cookie:` (todo el valor)
   - `x-csrf-token:`
5. Pégalos en `config.json` (campos `cookie` y `csrf`)
6. Reinicia el servidor

## Archivos

- `app.py` — servidor web (Flask) y endpoint `/api/resumen`
- `parrotfy.py` — cliente que consulta Parrotfy (movimientos tipo Egreso, Bodega Vending)
- `config.json` — sesión de Parrotfy (cookie + csrf)
- `templates/index.html` — dashboard
- `.venv` — entorno virtual con Flask, requests, beautifulsoup4
