# Registro operativo en Contabo

Completar este registro durante el primer despliegue autorizado. No contiene
secretos y no implica autorización para crear recursos o modificar DNS.

| Recurso | Valor previsto | Valor confirmado |
|---|---|---|
| Aplicación Coolify | `portfolio-repuestos-app` | `qsilh0hdmrcpyxinirg8y81y` |
| PostgreSQL exclusivo | `portfolio-repuestos-db` | `m12es2opa1h2iiu0iyp2cstc` |
| Dominio HTTPS | DuckDNS + Traefik | `https://repuestos-demo.blakyta3d.duckdns.org` |
| Dominio futuro | `repuestos-demo.metaia.pro` | Opcional, pendiente de DNS |
| Repositorio público | Proyecto independiente | `gagonzalez1/portfolio-repuestos-demo` |
| Rama de despliegue | `main` | Activa; autodeploy apagado |

## Actualización y rollback

1. Ejecutar la suite y la auditoría del catálogo.
2. Construir la imagen determinística y probar `/health` y `/demo`.
3. Desplegar primero al dominio temporal y ejecutar los smoke tests de
   `deploy/COOLIFY.md`.
4. Registrar aquí imagen, fecha, checksum del seed y dominio aprobado.
5. Ante una regresión, volver a la imagen estable anterior en Coolify; no
   eliminar la base. El esquema y el seed son idempotentes.

Primer despliegue estable: `zwz7stiqn418jrsgvl90fcj3`, 2026-08-07. Smoke
tests aprobados para `/`, `/demo`, `/health`, sesiones anónimas, consulta con
resultado, consulta inexistente y `/webhook` deshabilitado.
