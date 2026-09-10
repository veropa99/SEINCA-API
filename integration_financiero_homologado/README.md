# Módulo de Integración: Financiero -> Homologado

Este módulo permite enviar Pedidos de Venta y Pedidos de Compra desde una base de datos de Odoo (Financiero) a otra (Homologado) a través de la API XML-RPC.

## ⚙️ Configuración Requerida

Para que la integración funcione, es necesario realizar una configuración en **AMBAS** bases de datos.

### **Paso 1: En la Base de Datos de Destino (Odoo Homologado)**

Debemos crear un usuario dedicado para que la API se conecte. Esto mejora la seguridad al limitar los permisos a lo estrictamente necesario.

1.  **Crear un Usuario API:**
    * Ve a **Ajustes > Usuarios y Compañías > Usuarios**.
    * Crea un nuevo usuario. Ejemplo:
        * **Nombre:** `Usuario API Integración`
        * **Email / Login:** `api-user@tuempresa.com` (Usa un email real para notificaciones si es necesario).
        * **Tipo de Usuario:** `Usuario Interno`.

2.  **Asignar Permisos:**
    * Dale los permisos **mínimos necesarios**. No lo hagas administrador.
    * **Ventas:** Necesita poder `Crear` y `Leer` en el modelo `sale.order` y sus líneas, así como `Leer` en `res.partner` y `product.product`.
    * **Compras:** Necesita poder `Crear` y `Leer` en el modelo `purchase.order` y sus líneas.
    * **Importante:** Asegúrate de que este usuario tenga acceso a los Diarios, Almacenes y demás configuraciones que los pedidos puedan necesitar para ser validados.

3.  **Establecer una Contraseña Segura:**
    * Define una contraseña fuerte y guárdala. La necesitarás en el siguiente paso.

### **Paso 2: En la Base de Datos de Origen (Odoo Financiero)**

Aquí configuraremos los parámetros que el módulo usará para conectarse al Odoo Homologado.

1.  **Activa el Modo Desarrollador.**
    * Ve a **Ajustes** y haz clic en `Activar el modo desarrollador`.

2.  **Accede a los Parámetros del Sistema:**
    * Ve a **Ajustes > Técnico > Parámetros > Parámetros del Sistema**.

3.  **Crea las Siguientes Claves:**
    * Haz clic en `Crear` para cada una de las siguientes claves:

| Clave (Key)               | Valor (Value)                                                | Descripción                                                    |
| ------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------- |
| `homologado.db.url`       | `http://<ip_o_dominio_homologado>:8069`                      | La URL completa del servidor donde corre el Odoo Homologado.     |
| `homologado.db.name`      | `nombre_de_la_db_homologada`                                 | El nombre exacto de la base de datos de destino. Ejecuta: SELECT current_database();             |
| `homologado.db.user`      | `api-user@tuempresa.com`                                     | El login del usuario que creaste en el Paso 1.                 |
| `homologado.db.password`  | `la_clave_super_secreta_del_usuario_api`                     | La contraseña que definiste para el usuario API.               |
| `homologado.db.fixed_user_login`  | `vendedor.destino@tuempresa.com`                    | (Opcional) Login del usuario fijo que quedará asignado en ventas/compras creadas por integración. Si no se define, se usa `homologado.db.user`. |



**¡Importante!** Los valores que pongas aquí deben ser exactos, de lo contrario la conexión fallará.

## ✅ Verificación de Datos Maestros

Para que la integración no falle, los datos clave deben existir en **ambas** bases de datos y tener un identificador único consistente:

* **Clientes/Proveedores:** Deben tener su **RIF/Cédula (`vat`)** correctamente configurado en ambas DBs.
* **Productos:** Deben tener la misma **Referencia Interna (`default_code`)**.
* **Usuarios (Vendedores/Compradores):** Ya no es obligatorio que coincidan entre bases si configuras un usuario fijo en destino con `homologado.db.fixed_user_login`.

Si un registro no se encuentra en la base de datos de destino, el proceso se detendrá y mostrará un error informativo.