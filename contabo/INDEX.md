# Registro operativo en Contabo

Completar este registro durante el primer despliegue autorizado. No contiene
secretos y no implica autorización para crear recursos o modificar DNS.

| Recurso | Valor previsto | Valor confirmado |
|---|---|---|
| Aplicación Coolify | `portfolio-repuestos-app` | Pendiente |
| PostgreSQL exclusivo | `portfolio-repuestos-db` | Pendiente |
| Dominio temporal HTTPS | Asignado por Coolify | Pendiente |
| Dominio definitivo | `repuestos-demo.metaia.pro` | Pendiente de aprobación |
| Repositorio público | Proyecto independiente | Pendiente |
| Rama de despliegue | `main`, tras estabilizar | Pendiente |

## Actualización y rollback

1. Ejecutar la suite y la auditoría del catálogo.
2. Construir la imagen determinística y probar `/health` y `/demo`.
3. Desplegar primero al dominio temporal y ejecutar los smoke tests de
   `deploy/COOLIFY.md`.
4. Registrar aquí imagen, fecha, checksum del seed y dominio aprobado.
5. Ante una regresión, volver a la imagen estable anterior en Coolify; no
   eliminar la base. El esquema y el seed son idempotentes.
