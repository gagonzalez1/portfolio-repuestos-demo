# Validación previa al despliegue

Fecha local: 2026-08-07. Rama: `portfolio-demo`.

## Resultado automatizado

- Catálogo regenerado dos veces con salida idéntica.
- 300 productos con IDs `DEMO-0001` a `DEMO-0300`.
- Auditoría de privacidad aprobada: cero URLs/dominios, emails, teléfonos,
  referencias al comercio o identificadores/precios/stock originales.
- 30 consultas representativas aprobadas; el umbral automatizado es 90 %.
- Caso de búsqueda inexistente devuelve una lista vacía.
- 91 pruebas aprobadas y compilación de módulos Python correcta.
- `git diff --check` sin errores y búsqueda de patrones de secretos sin hallazgos.
- El dump fuente y el plan interno están ignorados y fuera del índice de Git.

Checksums reproducibles:

```text
demo_catalog.json       a89fee2c7031ded688417a97035f32361829afd5426a65a8c4df1545fac32af6
demo_catalog_audit.json 7e57c911cb7a2677dc8c117ec464566d62bbe5ff19047878300f1eb773e2105e
```

## Validaciones pendientes de autorización o infraestructura

- La imagen no se construyó localmente porque no hay CLI de Docker/Podman.
- No se crearon recursos en Coolify, PostgreSQL remoto, dominio, DNS ni
  autodeploy.
- No se rotaron o revocaron credenciales históricas: esa tarea requiere acceso
  y confirmación del propietario.
- El smoke test HTTPS y las capturas/video se realizan después de aprobar el
  dominio temporal.
