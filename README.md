# MotorIA — demo de búsqueda de repuestos

Demostración independiente de un agente especializado que interpreta consultas
cotidianas y busca por repuesto, vehículo, modelo, motor y medida. El catálogo,
los precios y la disponibilidad son ficticios. No es una tienda activa, no
procesa compras y no representa una relación comercial con una empresa concreta.

## Arquitectura

FastAPI sirve la experiencia web pública y el agente. `DemoCatalogClient`
consulta 300 productos anonimizados en un PostgreSQL exclusivo, reconstruible
desde `app/data/demo_catalog.json`. El runtime de portfolio no crea clientes de
comercio electrónico y el canal Meta permanece deshabilitado.

## Desarrollo local

Requiere Python 3.11 y PostgreSQL:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/seed_demo_catalog.py
uvicorn app.main:app --reload
```

La demo queda en `/demo`, el liveness check en `/health` y los paneles privados
en `/admin` y `/admin/params`. Arranca disponible 24/7 y el operador puede
pausarla con `DEMO_ENABLED` o con el kill switch del panel privado.

## Verificación

```bash
pytest -q
python scripts/build_demo_catalog.py --help
docker build -t portfolio-repuestos-demo .
```

El dump fuente es local e ignorado por Git. El constructor publica solo la
allowlist anónima y su informe de auditoría. Consultá
`docs/OPERATIONS.md`, `deploy/COOLIFY.md` y `docs/PORTFOLIO_CASE.md` para
operación, despliegue y presentación.
