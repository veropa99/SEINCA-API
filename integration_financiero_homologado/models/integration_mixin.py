# /integration_financiero_homologado/models/integration_mixin.py
import xmlrpc.client
import logging
import re
import time
import json
from odoo import models, fields, _, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class IntegrationMixin(models.AbstractModel):
    _name = "integration.mixin"
    _description = "Mixin para integración con DB Homologada"

    homologado_id = fields.Integer(
        string="ID Destino", readonly=True, copy=False, index=True
    )

    def _build_remote_error_message(self, action_label, error):
        """Convierte fallos XML-RPC remotos en mensajes accionables para usuario."""
        error_text = str(error)
        undefined_column_match = re.search(
            r'column "(?P<column>[^"]+)" of relation "(?P<relation>[^"]+)" does not exist',
            error_text,
        )

        if undefined_column_match:
            column_name = undefined_column_match.group("column")
            relation_name = undefined_column_match.group("relation")
            return _(
                "No se pudo %s porque la BD homologada tiene un desfase de esquema en la tabla '%s': falta la columna '%s'. "
                "Esto no depende de los valores enviados por esta integración; el servidor remoto está intentando usar un campo definido en código pero no creado en PostgreSQL. "
                "Actualice el módulo personalizado correspondiente en la BD destino y reinicie Odoo antes de reintentar."
            ) % (action_label, relation_name, column_name)

        if isinstance(error, xmlrpc.client.Fault):
            return _("No se pudo %s en la BD homologada: %s") % (
                action_label,
                error.faultString,
            )

        return _("No se pudo %s en la BD homologada: %s") % (action_label, error_text)

    def _get_homologado_credentials(self):
        """Obtiene las credenciales de los parámetros del sistema de forma segura."""
        config = self.env["ir.config_parameter"].sudo()
        url = config.get_param("homologado.db.url")
        db = config.get_param("homologado.db.name")
        username = config.get_param("homologado.db.user")
        password = config.get_param("homologado.db.password")

        if not all([url, db, username, password]):
            raise UserError(
                _(
                    "La configuración de la API para la base de datos homologada no está completa. Contacte al administrador."
                )
            )

        return url, db, username, password

    def _get_remote_models_proxy(self):
        """
        ✅ FUNCIÓN ACTUALIZADA: No se necesita un transporte personalizado.
        La clave es 'allow_none=True' en el ServerProxy.
        """
        url, db, username, password = self._get_homologado_credentials()
        try:
            # Ya no necesitamos la instancia de AllowNoneTransport

            common_url = f"{url}/xmlrpc/2/common"
            object_url = f"{url}/xmlrpc/2/object"

            # Pasamos 'allow_none=True' directamente al constructor
            common = xmlrpc.client.ServerProxy(
                common_url, verbose=False, allow_none=True  # <-- La clave
            )
            uid = common.authenticate(db, username, password, {})
            if not uid:
                raise UserError(
                    _(
                        "Autenticación fallida con la base de datos homologada. Verifique las credenciales."
                    )
                )

            models_proxy = xmlrpc.client.ServerProxy(
                object_url, verbose=False, allow_none=True  # <-- La clave
            )
            return models_proxy, db, uid, password
        except Exception as e:
            _logger.error("Error de conexión con Odoo Homologado: %s", str(e))
            raise UserError(
                _(f"Ocurrió un error al contactar la base de datos homologada:\n{e}")
            )

    def _find_remote_id(
        self, models_proxy, db, uid, password, model_name, search_fields, value
    ):
        """
        Encuentra el ID de un registro en la DB remota usando un valor en una lista de campos posibles.
        """
        if not value:
            raise UserError(
                _(
                    f"El valor de búsqueda para el modelo '{model_name}' está vacío. No se puede continuar."
                )
            )

        if isinstance(search_fields, str):
            search_fields = [search_fields]

        # ✅ CORRECCIÓN DEFINITIVA: Construir el dominio de forma plana (notación polaca).
        # Esto genera un dominio como: ['|', '|', ('vat', '=', V), ('identification_id', '=', V), ('rif', '=', V)]
        # Es una lista simple, sin anidaciones, y es la forma más robusta.
        domain = []
        # Añadimos un operador '|' por cada 'OR' que necesitamos. Para 3 campos, se necesitan 2 '|'.
        for i in range(len(search_fields) - 1):
            domain.append("|")
        # Ahora añadimos todas las condiciones de búsqueda (hojas).
        for field in search_fields:
            domain.append((field, "=", value))

        try:
            _logger.info(
                "Buscando en Homologado - Modelo: %s, Dominio: %s", model_name, domain
            )
            remote_ids = models_proxy.execute_kw(
                db, uid, password, model_name, "search", [domain], {"limit": 1}
            )

            if not remote_ids:
                raise UserError(
                    _(
                        f"No se encontró el registro en la DB Homologada:\n\n"
                        f"**Modelo:** `{model_name}`\n"
                        f"**Campos buscados:** `{', '.join(search_fields)}`\n"
                        f"**Valor buscado:** `{value}`"
                    )
                )
            return remote_ids[0]
        except Exception as e:
            _logger.error(
                "Error buscando ID remoto para %s con %s=%s: %s",
                model_name,
                search_fields,
                value,
                str(e),
            )
            # Analizamos si el error remoto es por un campo que no existe
            if "Invalid field" in str(e):
                raise UserError(
                    _(
                        "Error de configuración: Uno de los campos de búsqueda (%s) no existe en el modelo '%s' de la base de datos homologada."
                    )
                    % (", ".join(search_fields), model_name)
                )

            raise UserError(
                _(
                    "Error de comunicación buscando un registro relacionado en la base de datos de destino. Revise los logs. "
                    "Probablemente el contacto no existe en la Base de Datos destino."
                )
            )

    def _find_remote_product_id(self, models_proxy, db, uid, password, product):
        """
        Busca un product.product remoto:
        - Primero por referencia interna exacta (default_code).
        - Si no hay default_code, intenta por nombre exacto.
        Mensajes claros si falta referencia interna o no hay coincidencias.
        """
        if not product:
            raise UserError(
                _(
                    "No se proporcionó un producto para buscar en la Base de datos destino."
                )
            )

        name = product.name or product.display_name or ""
        code = product.default_code or False

        if not name:
            raise UserError(
                _("El producto seleccionado no tiene nombre definido."))

        # Dominio: por código o por nombre exacto
        if code:
            domain = ["|", ("default_code", "=", code), ("name", "=", name)]
        else:
            _logger.warning(
                "Producto sin referencia interna (default_code). Se intentará buscar por nombre exacto: %s",
                name,
            )
            domain = [("name", "=", name)]

        try:
            _logger.info(
                "Buscando en base de datos - Modelo: %s, Dominio: %s",
                "product.product",
                domain,
            )
            ids = models_proxy.execute_kw(
                db, uid, password, "product.product", "search", [
                    domain], {"limit": 1}
            )
            if not ids:
                if code:
                    raise UserError(
                        _(
                            "No se encontró el producto en la Base de datos destino por referencia interna '%s' ni por nombre exacto '%s'."
                        )
                        % (code, name)
                    )
                else:
                    raise UserError(
                        _(
                            "El producto '%s' no tiene Referencia Interna definida y tampoco se encontró un producto con nombre exacto en la Base de datos destino."
                        )
                        % (name,)
                    )
            return ids[0]
        except Exception as e:
            _logger.error(
                "Error buscando producto remoto por código/nombre: %s", str(e)
            )
            raise UserError(
                _(
                    "Error consultando la Base de datos destino para el producto '%s'. Detalle: %s"
                )
                % (name, str(e))
            )

    def _get_fixed_remote_user_id(self, models_proxy, db, uid, password):
        """
        Obtiene el usuario remoto fijo por login.
        Prioriza 'homologado.db.fixed_user_login' y, si no existe,
        usa el usuario API configurado en 'homologado.db.user'.
        """
        config = self.env["ir.config_parameter"].sudo()
        fixed_login = (config.get_param(
            "homologado.db.fixed_user_login") or "").strip()

        if not fixed_login:
            fixed_login = (config.get_param(
                "homologado.db.user") or "").strip()

        if not fixed_login:
            raise UserError(
                _(
                    "No hay un login de usuario fijo configurado para la integración. "
                    "Defina 'homologado.db.fixed_user_login' o revise 'homologado.db.user'."
                )
            )

        try:
            return self._find_remote_id(
                models_proxy,
                db,
                uid,
                password,
                "res.users",
                "login",
                fixed_login,
            )
        except UserError as e:
            raise UserError(
                _(
                    "No se encontró el usuario fijo '%s' en la base de datos destino. "
                    "Cree el usuario o ajuste la configuración.\nDetalle: %s"
                )
                % (fixed_login, str(e))
            )

    def _action_send_to_homologado_generic(
        self, remote_model, vals, confirm_method=None, invoice_method=None
    ):
        """
        Método genérico final para crear, confirmar y facturar el documento remoto.

        ✅ ACTUALIZADO:
        1. Se elimina el try/except para 'xmlrpc.client.Fault' (se asume allow_none=True).
        2. Se añade un bucle de reintento para la búsqueda de facturas y evitar 'race conditions'.
        """
        self.ensure_one()
        if self.homologado_id:
            raise UserError(
                _("Este documento ya fue enviado y registrado con el ID: %s")
                % self.homologado_id
            )

        models_proxy, db, uid, password = self._get_remote_models_proxy()

        # --- PASO 1: Crear el documento ---
        _logger.info(
            "Creando documento remoto en '%s' con valores: %s", remote_model, vals
        )
        try:
            new_remote_id = models_proxy.execute_kw(
                db, uid, password, remote_model, "create", [vals]
            )
        except Exception as e:
            msg = self._build_remote_error_message(
                _("crear el documento remoto"), e
            )
            self.message_post(body=msg)
            raise UserError(msg)

        # Obtener el nombre real del documento remoto
        remote_data = models_proxy.execute_kw(
            db, uid, password, remote_model, "read", [
                [new_remote_id], ["name"]]
        )
        remote_name = remote_data[0]["name"] if remote_data else self.name
        _logger.info("Nombre del documento remoto creado: %s", remote_name)

        self.write({"homologado_id": new_remote_id})
        self.message_post(
            body=_(
                f"✅ Documento enviado con éxito. ID Destino: {new_remote_id} ({remote_name})"
            )
        )

        # --- HOOK: Post-Creación ---
        # Se ejecuta después de crear el documento remoto, pero ANTES de confirmarlo.
        # Permite al modelo hijo forzar valores (como precios) que Odoo haya recalculado automáticamente.
        if hasattr(self, '_post_create_remote_hook'):
            try:
                self._post_create_remote_hook(
                    models_proxy, db, uid, password, new_remote_id)
            except Exception as hook_err:
                _logger.error(
                    f"Error ejecutando Hook Post-Creación: {hook_err}")
                self.message_post(
                    body=f"⚠️ Advertencia: Ocurrió un error forzando valores tras la creación: {hook_err}")

        # --- PASO 2: Confirmar el documento ---
        if confirm_method:
            try:
                _logger.info(
                    "Confirmando documento remoto ID %s con método '%s'",
                    new_remote_id,
                    confirm_method,
                )
                models_proxy.execute_kw(
                    db, uid, password, remote_model, confirm_method, [
                        [new_remote_id]]
                )
                self.message_post(
                    body=_("✅ Documento confirmado en la base de datos destino.")
                )
            except Exception as e:
                msg = self._build_remote_error_message(
                    _("confirmar el documento remoto"), e
                )
                self.message_post(body=msg)
                raise UserError(msg)

        # --- PASO 3: Crear la factura borrador ---
        if invoice_method:
            try:
                _logger.info(
                    "Iniciando creación de factura remota para ID %s", new_remote_id
                )

                # Lógica para Ventas (sale.order)
                if remote_model == "sale.order":
                    context = {
                        "active_model": "sale.order",
                        "active_ids": [new_remote_id],
                    }
                    wizard_vals = {"advance_payment_method": "delivered"}
                    wizard_id = models_proxy.execute_kw(
                        db,
                        uid,
                        password,
                        "sale.advance.payment.inv",
                        "create",
                        [wizard_vals],
                        {"context": context},
                    )

                    # ✅ CORRECCIÓN: Llamada directa. Ya no se espera un error 'Fault'.
                    # Esta llamada devolverá 'None' (o una acción) y no fallará gracias a 'allow_none=True'.
                    # ✅ CORRECCIÓN: Llamada protegida contra 'cannot marshal None'
                    try:
                        models_proxy.execute_kw(
                            db,
                            uid,
                            password,
                            "sale.advance.payment.inv",
                            "create_invoices",
                            [wizard_id],
                            {"context": context},
                        )
                    except xmlrpc.client.Fault as e:
                        if "cannot marshal None" in str(e):
                            _logger.warning(
                                "Ignorando error de serialización XML-RPC (None return) en create_invoices: %s",
                                e,
                            )
                        else:
                            raise e

                    # ✅ PRO-TIP: Búsqueda robusta con reintentos para evitar 'race conditions'
                    invoice_ids = []
                    search_domain = [
                        [
                            ("invoice_origin", "=", remote_name),
                            ("move_type", "=", "out_invoice"),
                        ]
                    ]
                    max_retries = 5
                    retry_delay_seconds = 5

                    for attempt in range(max_retries):
                        _logger.info(
                            f"Buscando factura remota (Intento {attempt + 1}/{max_retries})..."
                        )
                        invoice_ids = models_proxy.execute_kw(
                            db,
                            uid,
                            password,
                            "account.move",
                            "search",
                            search_domain,
                            {"limit": 1},
                        )
                        if invoice_ids:
                            _logger.info(
                                f"¡Factura remota encontrada! ID: {invoice_ids[0]}"
                            )
                            break  # ¡Encontrada! Salir del bucle.

                        if attempt < max_retries - 1:
                            time.sleep(
                                retry_delay_seconds
                            )  # Esperar antes de reintentar

                    # Comprobación final después de todos los reintentos
                    if not invoice_ids:
                        raise UserError(
                            _(
                                "Se ejecutó la creación de factura, pero no se pudo encontrar en la Base de datos destino para el origen %s después de %s intentos. Verifique manually."
                            )
                            % (self.name, max_retries)
                        )

                    self.write({"homologado_invoice_id": invoice_ids[0]})

                    # ✅ NUEVA FUNCIONALIDAD: Replicar cuentas contables de la factura
                    self._replicate_invoice_accounts(
                        models_proxy,
                        db,
                        uid,
                        password,
                        invoice_ids[0],
                        invoice_type="out_invoice",
                    )

                    self.message_post(
                        body=_(
                            "✅ Factura borrador creada en la Base de datos destino. ID Factura: %s"
                        )
                        % invoice_ids[0]
                    )

                # Lógica para Compras (purchase.order)
                elif remote_model == "purchase.order":
                    # PASO 3a: Ejecutar el método de creación de factura (ej. 'action_create_invoice')
                    # PASO 3a: Ejecutar el método de creación de factura (ej. 'action_create_invoice')
                    try:
                        models_proxy.execute_kw(
                            db,
                            uid,
                            password,
                            remote_model,
                            invoice_method,
                            [[new_remote_id]],
                        )
                    except xmlrpc.client.Fault as e:
                        if "cannot marshal None" in str(e):
                            _logger.warning(
                                "Ignorando error de serialización XML-RPC (None return) en %s: %s",
                                invoice_method,
                                e,
                            )
                        else:
                            raise e

                    # PASO 3b: Búsqueda robusta (igual que en ventas, pero con 'in_invoice')
                    created_invoice_ids = []
                    search_domain = [
                        [
                            ("invoice_origin", "=", remote_name),
                            ("move_type", "=", "in_invoice"),
                        ]
                    ]
                    max_retries = 5
                    retry_delay_seconds = 5

                    for attempt in range(max_retries):
                        _logger.info(
                            f"Buscando factura de proveedor remota (Intento {attempt + 1}/{max_retries})..."
                        )
                        created_invoice_ids = models_proxy.execute_kw(
                            db,
                            uid,
                            password,
                            "account.move",
                            "search",
                            search_domain,
                            {"limit": 1},
                        )
                        if created_invoice_ids:
                            _logger.info(
                                f"¡Factura de proveedor remota encontrada! ID: {created_invoice_ids[0]}"
                            )
                            break
                        if attempt < max_retries - 1:
                            time.sleep(retry_delay_seconds)

                    if not created_invoice_ids:
                        raise UserError(
                            _(
                                "Se ejecutó la creación de factura de proveedor, pero no se pudo encontrar en la BD homologada para el origen %s después de %s intentos. Verifique manualmente."
                            )
                            % (self.name, max_retries)
                        )

                    self.write(
                        {"homologado_invoice_id": created_invoice_ids[0]})

                    # ✅ NUEVA FUNCIONALIDAD: Replicar cuentas contables de la factura
                    self._replicate_invoice_accounts(
                        models_proxy,
                        db,
                        uid,
                        password,
                        created_invoice_ids[0],
                        invoice_type="in_invoice",
                    )

                    self.message_post(
                        body=_(
                            "✅ Factura de proveedor borrador creada en la Base de datos destino. ID Factura: %s"
                        )
                        % created_invoice_ids[0]
                    )

            except Exception as e:
                msg = self._build_remote_error_message(
                    _("crear la factura borrador remota"), e
                )
                self.message_post(body=msg)
                raise UserError(msg)

        # Forzar recarga de la vista para re-evaluar attrs/invisible del botón
        return {"type": "ir.actions.client", "tag": "reload"}

    def _remote_fields_info(self, models_proxy, db, uid, password, model_name, attributes=None):
        """Devuelve dict con la información de campos existentes en el modelo remoto."""
        attrs = attributes or ["type", "required", "selection"]
        return models_proxy.execute_kw(
            db, uid, password, model_name, "fields_get", [], {"attributes": attrs}
        )

    def _remote_fields(self, models_proxy, db, uid, password, model_name):
        """Devuelve set de campos existentes en el modelo remoto."""
        info = models_proxy.execute_kw(
            db, uid, password, model_name, "fields_get", [], {
                "attributes": ["type"]}
        )
        return set(info.keys())

    def _filter_remote_vals(self, vals, remote_fields):
        """Quita del dict los campos que no existen en remoto o que vienen None."""
        clean = {}
        for k, v in vals.items():
            if k in remote_fields and v is not None:
                clean[k] = v
        return clean

    def _get_or_create_remote_uom(self, models_proxy, db, uid, password, uom):
        """
        Busca o crea una UoM remota (y su categoría) por nombre.
        Si la UoM no es de referencia, asegura que la UoM de referencia de la categoría
        se sincronice primero para evitar errores de restricción en Odoo.
        """
        if not uom:
            return False

        # --- 0. Pre-requisito: Si no es referencia, sincronizar la referencia primero ---
        # Esto es vital porque Odoo exige que una categoría tenga una unidad de referencia
        # antes de crear otras unidades (bigger/smaller).
        if uom.uom_type != "reference":
            reference_uom = self.env["uom.uom"].search(
                [
                    ("category_id", "=", uom.category_id.id),
                    ("uom_type", "=", "reference"),
                ],
                limit=1,
            )

            if reference_uom and reference_uom.id != uom.id:
                try:
                    _logger.info(
                        "Sincronizando UoM de referencia '%s' antes de '%s'",
                        reference_uom.name,
                        uom.name,
                    )
                    self._get_or_create_remote_uom(
                        models_proxy, db, uid, password, reference_uom
                    )
                except Exception as e:
                    _logger.warning(
                        "No se pudo sincronizar la UoM de referencia anterior: %s", e
                    )
                    # No lanzamos error aquí para permitir intentar crear la actual si fuera posible,
                    # aunque probablemente fallará más adelante.

        # 1. Buscar UoM
        domain = [("name", "=", uom.name)]
        ids = models_proxy.execute_kw(
            db, uid, password, "uom.uom", "search", [domain], {"limit": 1}
        )
        if ids:
            return ids[0]

        _logger.warning(
            "UoM '%s' no encontrada en destino. Creando...", uom.name)

        try:
            # 2. Buscar o Crear Categoría de UoM
            cat_remote_id = False
            if uom.category_id:
                cat_domain = [("name", "=", uom.category_id.name)]
                cat_ids = models_proxy.execute_kw(
                    db,
                    uid,
                    password,
                    "uom.category",
                    "search",
                    [cat_domain],
                    {"limit": 1},
                )
                if cat_ids:
                    cat_remote_id = cat_ids[0]
                else:
                    cat_vals = {"name": uom.category_id.name}
                    cat_remote_id = models_proxy.execute_kw(
                        db, uid, password, "uom.category", "create", [cat_vals]
                    )
                    _logger.info(
                        "✅ Categoría UoM creada en destino: %s (ID: %s)",
                        uom.category_id.name,
                        cat_remote_id,
                    )

            # 3. Crear UoM
            vals = {
                "name": uom.name,
                "category_id": cat_remote_id,
                "uom_type": uom.uom_type,
                "factor": uom.factor,
                "rounding": uom.rounding,
                "active": True,
            }
            # Unidades de referencia tienen factor 1.0 (en realidad factor inv) pero Odoo lo maneja.
            # Al crear, si es reference, factor debe ser 1.0.

            new_id = models_proxy.execute_kw(
                db, uid, password, "uom.uom", "create", [vals]
            )
            _logger.info("✅ UoM creada en destino: %s (ID: %s)",
                         uom.name, new_id)
            return new_id
        except Exception as e:
            _logger.error("Error creando UoM remota '%s': %s",
                          uom.name, str(e))
            # Fallback crítica: si no tenemos UoM, fallará la creación del producto.
            raise UserError(
                _(
                    "No se pudo sincronizar la Unidad de Medida '%s' necesaria para el producto.\nError: %s"
                )
                % (uom.name, str(e))
            )

    def _get_or_create_remote_product(
        self, models_proxy, db, uid, password, product, line_uom=None, default_price=None
    ):
        """
        Busca producto remoto por default_code o barcode (o name).
        Si no existe, lo crea con campos permitidos y asegura campos obligatorios
        (como list_price_usd y requeridos por localizaciones/personalizaciones).
        Omite campos que no existan en destino.
        """
        if not product:
            raise UserError(_("No se proporcionó un producto."))

        remote_model = "product.product"
        remote_fields_info = self._remote_fields_info(
            models_proxy, db, uid, password, remote_model
        )
        remote_fields = set(remote_fields_info.keys())

        name = product.name or product.display_name
        code = product.default_code or False
        barcode = product.barcode or False

        # --- 1) BUSCAR ---
        domain = []
        if code and barcode:
            domain = ["|", ("default_code", "=", code),
                      ("barcode", "=", barcode)]
        elif code:
            domain = [("default_code", "=", code)]
        elif barcode:
            domain = [("barcode", "=", barcode)]
        else:
            domain = [("name", "=", name)]

        ids = models_proxy.execute_kw(
            db, uid, password, remote_model, "search", [domain], {"limit": 1}
        )
        if ids:
            return ids[0]

        # --- 2) CREAR ---
        # Helpers para M2O/M2M
        def find_remote_id(model, field, value):
            if not value:
                return False
            rid = models_proxy.execute_kw(
                db,
                uid,
                password,
                model,
                "search",
                [[(field, "=", value)]],
                {"limit": 1},
            )
            return rid[0] if rid else False

        def find_remote_ids(model, field, values):
            if not values:
                return []
            rids = models_proxy.execute_kw(
                db, uid, password, model, "search", [[(field, "in", values)]]
            )
            return rids or []

        # categ_id (M2O) por nombre
        categ_remote_id = False
        if product.categ_id:
            categ_remote_id = find_remote_id(
                "product.category", "name", product.categ_id.name
            )
            if not categ_remote_id:
                _logger.warning(
                    "Categoría '%s' no encontrada en destino. Creando...",
                    product.categ_id.name,
                )
                try:
                    # Intentamos crear la categoría simple (sin padre por ahora para evitar recursión compleja)
                    categ_vals = {"name": product.categ_id.name}
                    categ_remote_id = models_proxy.execute_kw(
                        db, uid, password, "product.category", "create", [
                            categ_vals]
                    )
                    _logger.info(
                        "✅ Categoría creada en destino: %s (ID: %s)",
                        product.categ_id.name,
                        categ_remote_id,
                    )
                except Exception as e:
                    _logger.error(
                        "Error creando categoría remota '%s': %s",
                        product.categ_id.name,
                        str(e),
                    )
                    # Fallback (opcional): Buscar una categoría por defecto o dejar que falle si es obligatoria

        # product_tag_ids (M2M) por nombre
        tag_names = (
            product.product_tag_ids.mapped(
                "name") if product.product_tag_ids else []
        )
        tag_remote_ids = find_remote_ids("product.tag", "name", tag_names)

        # UoM (M2O) por nombre (usando helper que crea si no existe)
        # Si la llamada proviene de una línea de pedido y se pasó `line_uom`,
        # usamos esa UoM para el template/producto; en caso contrario usamos
        # la UoM definida en el producto.
        uom_source = line_uom or product.uom_id
        uom_remote_id = False
        if uom_source:
            uom_remote_id = self._get_or_create_remote_uom(
                models_proxy, db, uid, password, uom_source
            )
        uom_po_remote_id = self._get_or_create_remote_uom(
            models_proxy, db, uid, password, product.uom_po_id
        )

        # Precios base y USD (crucial para entornos bimonetarios o localizaciones con list_price_usd)
        product_list_price = getattr(product, "list_price", 0.0) or 0.0
        product_standard_price = getattr(product, "standard_price", 0.0) or 0.0

        product_list_price_usd = (
            getattr(product, "list_price_usd", False)
            or getattr(product, "lst_price_usd", False)
            or (default_price if (default_price is not None and default_price > 0) else False)
            or product_list_price
            or 0.0
        )
        product_standard_price_usd = (
            getattr(product, "standard_price_usd", False)
            or getattr(product, "cost_usd", False)
            or product_standard_price
            or 0.0
        )

        vals = {
            "detailed_type": getattr(product, "detailed_type", None),
            "name": name,
            "barcode": barcode,
            "default_code": code,
            "categ_id": categ_remote_id,
            "product_tag_ids": [(6, 0, tag_remote_ids)] if tag_remote_ids else None,
            "weight": product.weight if product.weight is not False else None,
            "volume": product.volume if product.volume is not False else None,
            "sale_ok": product.sale_ok,
            "purchase_ok": product.purchase_ok,
            "uom_id": uom_remote_id,
            "uom_po_id": uom_po_remote_id,
            "list_price": product_list_price,
            "standard_price": product_standard_price,
            "list_price_usd": product_list_price_usd,
            "standard_price_usd": product_standard_price_usd,
        }

        # Asegurar valores para campos marcados como obligatorios (required=True) en el modelo remoto
        for fname, finfo in remote_fields_info.items():
            if finfo.get("required") and (fname not in vals or vals[fname] is None or vals[fname] is False):
                # 1. Intentar tomar del producto local si existe
                if hasattr(product, fname):
                    local_val = getattr(product, fname)
                    if local_val is not None and local_val is not False:
                        vals[fname] = local_val
                        continue
                # 2. Asignación según el campo o tipo de dato
                if fname == "list_price_usd":
                    vals[fname] = product_list_price_usd
                elif fname == "standard_price_usd":
                    vals[fname] = product_standard_price_usd
                elif finfo.get("type") in ("float", "monetary"):
                    vals[fname] = 0.0
                elif finfo.get("type") == "integer":
                    vals[fname] = 0
                elif finfo.get("type") in ("char", "text"):
                    vals[fname] = name or "-"
                elif finfo.get("type") == "boolean":
                    vals[fname] = False
                elif finfo.get("type") == "selection" and finfo.get("selection"):
                    vals[fname] = finfo["selection"][0][0]

        vals = self._filter_remote_vals(vals, remote_fields)

        new_id = models_proxy.execute_kw(
            db, uid, password, remote_model, "create", [vals]
        )
        _logger.info("Producto creado en destino: %s (%s)", name, new_id)
        # --- Sincronizar impuestos y campos del template del producto ---
        try:
            # Obtener template local y sus impuestos
            local_tmpl = getattr(product, "product_tmpl_id", None) or product
            sale_taxes = getattr(local_tmpl, "taxes_id", False)
            purchase_taxes = getattr(local_tmpl, "supplier_taxes_id", False)

            sale_remote_tax_ids = []
            purchase_remote_tax_ids = []

            if sale_taxes:
                sale_remote_tax_ids = self._map_remote_taxes(
                    models_proxy, db, uid, password, sale_taxes, usage="sale"
                )

            if purchase_taxes:
                purchase_remote_tax_ids = self._map_remote_taxes(
                    models_proxy, db, uid, password, purchase_taxes, usage="purchase"
                )

            # Obtener el product_tmpl_id creado en remoto
            tmpl_info = models_proxy.execute_kw(
                db, uid, password, "product.product", "read", [
                    [new_id], ["product_tmpl_id"]]
            )
            tmpl_id = False
            if tmpl_info and isinstance(tmpl_info, list) and tmpl_info[0].get("product_tmpl_id"):
                # product_tmpl_id puede venir como [id, name]
                pt = tmpl_info[0]["product_tmpl_id"]
                tmpl_id = pt[0] if isinstance(pt, (list, tuple)) and pt else pt

            if tmpl_id:
                template_remote_fields = self._remote_fields(
                    models_proxy, db, uid, password, "product.template"
                )
                write_vals = {}
                if sale_remote_tax_ids and "taxes_id" in template_remote_fields:
                    write_vals["taxes_id"] = [(6, 0, sale_remote_tax_ids)]
                if purchase_remote_tax_ids and "supplier_taxes_id" in template_remote_fields:
                    write_vals["supplier_taxes_id"] = [
                        (6, 0, purchase_remote_tax_ids)]

                # Si se creó con una UoM derivada de la línea, forzamos la UoM
                # en el template remoto cuando el campo exista.
                if uom_remote_id and "uom_id" in template_remote_fields:
                    write_vals["uom_id"] = uom_remote_id
                if uom_po_remote_id and "uom_po_id" in template_remote_fields:
                    write_vals["uom_po_id"] = uom_po_remote_id

                if "list_price_usd" in template_remote_fields and product_list_price_usd:
                    write_vals["list_price_usd"] = product_list_price_usd
                if "standard_price_usd" in template_remote_fields and product_standard_price_usd:
                    write_vals["standard_price_usd"] = product_standard_price_usd

                if write_vals:
                    try:
                        models_proxy.execute_kw(
                            db, uid, password, "product.template", "write", [
                                [tmpl_id], write_vals]
                        )
                        _logger.info(
                            "Valores del template sincronizados en destino (template_id=%s): %s",
                            tmpl_id,
                            write_vals,
                        )
                    except Exception as e:
                        _logger.warning(
                            "No se pudo escribir campos en product.template remoto: %s", e)
        except Exception as e:
            _logger.warning(
                "Error sincronizando impuestos de template para producto '%s': %s", name, e)
        return new_id

    def _get_or_create_remote_partner(self, models_proxy, db, uid, password, partner):
        """
        Busca partner remoto por vat/rif/identification_id.
        Si no existe, lo crea con campos permitidos.
        Para Individuos, SIEMPRE mapea y formatea tanto la Cédula (identification_id) como el RIF.
        """
        if not partner:
            raise UserError(_("No se proporcionó un partner."))

        remote_model = "res.partner"
        remote_fields = self._remote_fields(
            models_proxy, db, uid, password, remote_model
        )

        is_company = getattr(partner, "is_company", False)
        raw_vat = partner.vat or getattr(partner, "rif", "") or getattr(
            partner, "identification_id", "")

        # --- 1) LÓGICA DE FORMATEO INTELIGENTE DE RIF Y CÉDULA (VE) ---
        ident_prefix = ""
        try:
            # Extraer la letra del combobox en origen si existe
            ident_type = getattr(
                partner, "l10n_latam_identification_type_id", False)
            if ident_type and ident_type.name:
                ident_name = ident_type.name.upper()
                if "RIF J" in ident_name or "J-" in ident_name or ident_name == 'J':
                    ident_prefix = 'J'
                elif "RIF V" in ident_name or "V-" in ident_name or ident_name == 'V':
                    ident_prefix = 'V'
                elif "RIF E" in ident_name or "E-" in ident_name or ident_name == 'E':
                    ident_prefix = 'E'
                elif "RIF G" in ident_name or "G-" in ident_name or ident_name == 'G':
                    ident_prefix = 'G'
                elif "RIF C" in ident_name or "C-" in ident_name or ident_name == 'C':
                    ident_prefix = 'C'
                elif "PASAPORTE" in ident_name or " P " in ident_name or ident_name == 'P':
                    ident_prefix = 'P'
        except Exception:
            pass

        formatted_rif = False
        formatted_identification_id = False
        nationality = getattr(partner, "nationality", "V")

        if raw_vat:
            clean_vat = str(raw_vat).strip().upper()
            letter = ""

            # Identificar letra inicial
            if clean_vat and clean_vat[0] in ['V', 'E', 'J', 'G', 'C', 'P']:
                letter = clean_vat[0]
                clean_num_only = re.sub(r'[^0-9]', '', clean_vat[1:])
            else:
                letter = ident_prefix
                clean_num_only = re.sub(r'[^0-9]', '', clean_vat)

            # Fallback seguro
            if not letter:
                letter = 'J' if is_company else 'V'

            # Definir la nacionalidad para validation_document_ident
            nationality = letter if letter in ['V', 'E', 'P'] else 'V'

            if is_company:
                # LÓGICA DE COMPAÑÍA
                if len(clean_num_only) >= 2:
                    body = clean_num_only[:-1]
                    check_digit = clean_num_only[-1]
                    if len(body) < 8:
                        body = body.zfill(8)
                    formatted_rif = f"{letter}-{body}-{check_digit}"
            else:
                # LÓGICA DE PERSONA NATURAL: SIEMPRE generar ambos datos
                if len(clean_num_only) >= 9:
                    # CASO A: Ingresaron el RIF completo. Extraemos Cédula y formateamos RIF.
                    body = clean_num_only[:-1]
                    check_digit = clean_num_only[-1]

                    cedula_num = str(int(body)) if body.isdigit() else body
                    if len(body) < 8:
                        body = body.zfill(8)

                    formatted_rif = f"{letter}-{body}-{check_digit}"
                    formatted_identification_id = cedula_num
                elif len(clean_num_only) > 0:
                    # CASO B: Ingresaron solo Cédula (8 dígitos o menos).
                    cedula_num = str(
                        int(clean_num_only)) if clean_num_only.isdigit() else clean_num_only
                    formatted_identification_id = cedula_num

                    # Construimos un RIF válido agregando un '0' como dígito verificador genérico
                    body = clean_num_only
                    if len(body) < 8:
                        body = body.zfill(8)

                    formatted_rif = f"{letter}-{body}-0"

        # Respaldo por si falló el Regex
        if not formatted_rif and getattr(partner, "rif", False):
            formatted_rif = partner.rif
        if not formatted_identification_id and getattr(partner, "identification_id", False):
            formatted_identification_id = partner.identification_id

        # --- 2) BUSCAR ---
        conditions = []
        if formatted_rif and "rif" in remote_fields:
            conditions.append(("rif", "=", formatted_rif))
        if formatted_rif and "vat" in remote_fields:
            conditions.append(("vat", "=", formatted_rif))
        if formatted_identification_id and "identification_id" in remote_fields:
            conditions.append(
                ("identification_id", "=", formatted_identification_id))

        if not conditions:
            conditions.append(("name", "=", partner.name))

        domain_or = []
        for i in range(len(conditions) - 1):
            domain_or.append("|")
        domain_or.extend(conditions)

        if "is_company" in remote_fields:
            domain = ["&", ("is_company", "=", is_company)] + domain_or
        else:
            domain = domain_or

        ids = models_proxy.execute_kw(
            db, uid, password, remote_model, "search", [domain], {"limit": 1}
        )
        if ids:
            return ids[0]

        # --- 3) CREAR ---
        def find_remote_id(model, field, value):
            if not value:
                return False
            rid = models_proxy.execute_kw(db, uid, password, model, "search", [
                                          [(field, "=", value)]], {"limit": 1})
            return rid[0] if rid else False

        def find_remote_ids(model, field, values):
            if not values:
                return []
            rids = models_proxy.execute_kw(db, uid, password, model, "search", [
                                           [(field, "in", values)]])
            return rids or []

        country_remote_id = False
        if partner.country_id:
            country_remote_id = find_remote_id("res.country", "code", partner.country_id.code) or find_remote_id(
                "res.country", "name", partner.country_id.name)

        municipality_remote_id = False
        if getattr(partner, "municipality_id", False):
            municipality_remote_id = find_remote_id(
                "res.country.state.municipality", "name", partner.municipality_id.name)

        parish_remote_id = False
        if getattr(partner, "parish_id", False):
            parish_remote_id = find_remote_id(
                "res.country.state.municipality.parish", "name", partner.parish_id.name)

        cat_names = partner.category_id.mapped(
            "name") if partner.category_id else []
        category_remote_ids = find_remote_ids(
            "res.partner.category", "name", cat_names)

        purchase_journal_id = False
        if getattr(partner, "purchase_journal_id", False):
            purchase_journal_id = find_remote_id(
                "account.journal", "code", partner.purchase_journal_id.code)

        purchase_sales_id = False
        if getattr(partner, "purchase_sales_id", False):
            purchase_sales_id = find_remote_id(
                "account.journal", "code", partner.purchase_sales_id.code)

        receivable_id = False
        if partner.property_account_receivable_id:
            receivable_id = find_remote_id(
                "account.account", "code", partner.property_account_receivable_id.code)

        payable_id = False
        if partner.property_account_payable_id:
            payable_id = find_remote_id(
                "account.account", "code", partner.property_account_payable_id.code)

        vals = {
            "name": partner.name,
            "company_type": getattr(partner, "company_type", None),

            # --- ASIGNACIÓN ESTRICTA ---
            "rif": formatted_rif,
            "vat": formatted_rif if is_company else (formatted_rif or formatted_identification_id or raw_vat),
            "identification_id": formatted_identification_id if not is_company else None,
            "nationality": nationality,

            "people_type_company": getattr(partner, "people_type_company", None),
            "people_type_individual": getattr(partner, "people_type_individual", None),

            "street": partner.street or None,
            "city": partner.city or None,
            "zip": partner.zip or None,
            "country_id": country_remote_id,
            "municipality_id": municipality_remote_id,
            "parish_id": parish_remote_id,

            "phone": partner.phone or None,
            "email": partner.email or None,
            "category_id": [(6, 0, category_remote_ids)] if category_remote_ids else None,
            "purchase_journal_id": purchase_journal_id,
            "purchase_sales_id": purchase_sales_id,
            "property_account_receivable_id": receivable_id,
            "property_account_payable_id": payable_id,

            "vat_subjected": getattr(partner, "vat_subjected", None),
            "wh_iva_agent": getattr(partner, "wh_iva_agent", None),
            "wh_iva_rate": getattr(partner, "wh_iva_rate", None),
            "islr_withholding_agent": getattr(partner, "islr_withholding_agent", None),
            "contribuyente_seniat": getattr(partner, "contribuyente_seniat", None),
        }

        vals = self._filter_remote_vals(vals, remote_fields)

        new_id = models_proxy.execute_kw(
            db, uid, password, remote_model, "create", [vals])
        _logger.info("Partner creado en destino: %s (%s)",
                     partner.name, new_id)
        return new_id

    def _map_remote_taxes(self, models_proxy, db, uid, password, taxes, usage=None):
        """
        Mapea impuestos locales a IDs remotos intentando emparejar por:
        1) `name` + `type_tax_use` (si `usage` es proporcionado: 'sale'|'purchase')
        2) `name` solamente
        3) `amount` como último recurso

        Devuelve lista de IDs remotos (mantiene el orden y evita duplicados).
        """
        if not taxes:
            return []

        # Asegurar iterable de taxes (recordset o lista)
        try:
            iterable = list(taxes)
        except Exception:
            iterable = [taxes]

        mapped = []
        try:
            for t in iterable:
                # extraer propiedades del tax local
                try:
                    name = getattr(t, "name", None)
                except Exception:
                    name = None
                try:
                    amount = getattr(t, "amount", None)
                except Exception:
                    amount = None

                found_id = False

                # 1) Buscar por name + usage
                if name:
                    if usage:
                        domain = [("name", "=", name),
                                  ("type_tax_use", "=", usage)]
                        r = models_proxy.execute_kw(
                            db, uid, password, "account.tax", "search", [
                                domain], {"limit": 1}
                        )
                        if r:
                            found_id = r[0]
                    # 2) Buscar por name solo
                    if not found_id:
                        domain = [("name", "=", name)]
                        r = models_proxy.execute_kw(
                            db, uid, password, "account.tax", "search", [
                                domain], {"limit": 1}
                        )
                        if r:
                            found_id = r[0]

                # 3) Fallback por amount
                if not found_id and amount is not None:
                    try:
                        amt = float(amount)
                        domain = [("amount", "=", amt)]
                        if usage:
                            domain_with_usage = domain + \
                                [("type_tax_use", "=", usage)]
                            r = models_proxy.execute_kw(
                                db, uid, password, "account.tax", "search", [
                                    domain_with_usage], {"limit": 1}
                            )
                            if r:
                                found_id = r[0]
                        if not found_id:
                            r = models_proxy.execute_kw(
                                db, uid, password, "account.tax", "search", [
                                    domain], {"limit": 1}
                            )
                            if r:
                                found_id = r[0]
                    except Exception:
                        pass

                if found_id and found_id not in mapped:
                    mapped.append(found_id)
        except Exception as e:
            _logger.warning("Error mapeando impuestos remotos: %s", e)

        return mapped

    def _find_remote_account_by_code(self, models_proxy, db, uid, password, account_code):
        """
        Busca una cuenta contable remota por su código.
        Devuelve el ID de la cuenta remota o False si no se encuentra.
        """
        if not account_code:
            return False

        try:
            domain = [("code", "=", account_code)]
            account_ids = models_proxy.execute_kw(
                db, uid, password, "account.account", "search", [
                    domain], {"limit": 1}
            )
            if account_ids:
                return account_ids[0]
        except Exception as e:
            _logger.warning(
                "Error buscando cuenta remota con código '%s': %s",
                account_code,
                str(e),
            )

        return False

    def _find_remote_analytic_by_code(self, models_proxy, db, uid, password, analytic_code):
        """
        Busca una cuenta analítica remota por su código.
        Devuelve el ID de la cuenta analítica remota o False si no se encuentra.
        """
        if not analytic_code:
            return False

        try:
            domain = [("code", "=", analytic_code)]
            analytic_ids = models_proxy.execute_kw(
                db, uid, password, "account.analytic.account", "search", [
                    domain], {"limit": 1}
            )
            if analytic_ids:
                return analytic_ids[0]
        except Exception as e:
            _logger.warning(
                "Error buscando cuenta analítica remota con código '%s': %s",
                analytic_code,
                str(e),
            )

        return False

    def _find_remote_analytic_by_name(self, models_proxy, db, uid, password, analytic_name):
        """
        Busca una cuenta analítica remota por su nombre.
        Devuelve el ID de la cuenta analítica remota o False si no se encuentra.
        """
        if not analytic_name:
            return False

        try:
            domain = [("name", "=", analytic_name)]
            analytic_ids = models_proxy.execute_kw(
                db, uid, password, "account.analytic.account", "search", [
                    domain], {"limit": 1}
            )
            if analytic_ids:
                return analytic_ids[0]
        except Exception as e:
            _logger.warning(
                "Error buscando cuenta analítica remota con nombre '%s': %s",
                analytic_name,
                str(e),
            )

        return False

    def _get_or_create_remote_analytic(
        self, models_proxy, db, uid, password, analytic_account
    ):
        """
        Busca una cuenta analítica remota por código O nombre.
        Si no existe, la CREA automáticamente en la BD destino.

        ✅ OBJETIVO: Sincronizar cuentas analíticas faltantes automáticamente.
        ✅ MEJORADO: Trabaja con o sin código (usa nombre como fallback).

        Estrategia de búsqueda:
        1. Si tiene código → Buscar por código
        2. Si NO tiene código → Buscar por nombre
        3. Si no existe → Crear con los datos disponibles

        Args:
            models_proxy: Proxy XML-RPC de la BD destino
            db: Nombre de la BD destino
            uid: ID del usuario autenticado en destino
            password: Contraseña del usuario autenticado
            analytic_account: Objeto account.analytic.account desde BD origen

        Returns:
            ID de la cuenta analítica remota, o False si no se puede crear
        """
        if not analytic_account:
            return False

        try:
            code = analytic_account.code or ""
            name = analytic_account.name or ""

            if not code and not name:
                _logger.warning(
                    "Cuenta analítica (ID: %s) no tiene código ni nombre. No se puede sincronizar.",
                    analytic_account.id,
                )
                return False

            # --- PASO 1: Intentar buscar por CÓDIGO (si existe) ---
            remote_analytic_id = False
            search_by = "nombre"

            if code:
                remote_analytic_id = self._find_remote_analytic_by_code(
                    models_proxy, db, uid, password, code
                )
                search_by = "código"

                if remote_analytic_id:
                    _logger.info(
                        "Cuenta analítica remota encontrada por código: %s (Código: %s, ID: %s)",
                        name,
                        code,
                        remote_analytic_id,
                    )
                    return remote_analytic_id

            # --- PASO 2: Si no encontró por código, intentar por NOMBRE ---
            if not remote_analytic_id and name:
                remote_analytic_id = self._find_remote_analytic_by_name(
                    models_proxy, db, uid, password, name
                )

                if remote_analytic_id:
                    _logger.info(
                        "Cuenta analítica remota encontrada por nombre: %s (Nombre: %s, ID: %s)",
                        name,
                        name,
                        remote_analytic_id,
                    )
                    return remote_analytic_id

            # --- PASO 3: Si no existe, CREAR en destino ---
            _logger.warning(
                "Cuenta analítica '%s' (Código: %s) no encontrada en destino por %s. Creando...",
                name,
                code if code else "SIN CÓDIGO",
                search_by,
            )

            vals = {
                "name": name,
                "active": True,
            }

            # Agregar código solo si existe
            if code:
                vals["code"] = code

            # Obtener campos remotos disponibles para filtrar
            remote_fields = self._remote_fields(
                models_proxy, db, uid, password, "account.analytic.account"
            )
            vals = self._filter_remote_vals(vals, remote_fields)

            # Crear la cuenta analítica remota
            new_id = models_proxy.execute_kw(
                db,
                uid,
                password,
                "account.analytic.account",
                "create",
                [vals],
            )

            _logger.info(
                "✅ Cuenta analítica creada en destino: %s (Código: %s, ID Remoto: %s)",
                name,
                code if code else "SIN CÓDIGO",
                new_id,
            )
            return new_id

        except Exception as e:
            _logger.error(
                "Error creando/buscando cuenta analítica remota para '%s': %s",
                analytic_account.name if analytic_account else "Unknown",
                str(e),
            )
            return False

    def _process_analytic_distribution(
        self, models_proxy, db, uid, password, origin_line
    ):
        """
        Procesa la distribución analítica de una línea de factura origen.

        Convierte el campo `analytic_distribution` (origen) al mismo campo
        `analytic_distribution` (destino) en formato dict con IDs remotos.

        ✅ LANZA ERROR: Si una analítica NO existe en destino

        Formato:
        ├─ Origen: {"1": 50.0, "2": 50.0}  (ID origen: porcentaje)
        └─ Destino: {"45": 50.0, "46": 50.0}  (ID remoto: porcentaje)

        Args:
            models_proxy: Proxy XML-RPC de la BD destino
            db: Nombre de la BD destino
            uid: ID del usuario autenticado en destino
            password: Contraseña del usuario autenticado
            origin_line: Línea de factura desde la BD origen

        Returns:
            Dict con formato {"id_remoto": porcentaje} o {} si vacío

        Raises:
            UserError: Si una analítica de la distribución no existe en destino
        """
        try:
            # Obtener el campo analytic_distribution de origen
            analytic_dist = getattr(origin_line, "analytic_distribution", {})

            if not analytic_dist:
                _logger.debug(
                    "Línea %s no tiene distribución analítica definida.",
                    origin_line.id,
                )
                return {}

            # analytic_distribution puede ser un dict como: {"1": 50.0, "2": 50.0}
            # o un JSON string que debemos parsear
            if isinstance(analytic_dist, str):
                try:
                    analytic_dist = json.loads(analytic_dist)
                except (json.JSONDecodeError, TypeError):
                    _logger.warning(
                        "No se pudo parsear analytic_distribution como JSON: %s",
                        analytic_dist,
                    )
                    return {}

            if not isinstance(analytic_dist, dict):
                _logger.warning(
                    "analytic_distribution no es un diccionario válido: %s",
                    analytic_dist,
                )
                return {}

            # Procesar cada entrada: account_id -> percentage
            # Devolvemos un dict con IDs remotos mapeados
            distribution_dict = {}
            missing_analytics = []

            for analytic_account_id_local, percentage in analytic_dist.items():
                # analytic_account_id_local es el ID de la cuenta analítica en origen
                # Buscamos la cuenta analítica local para obtener su código/nombre
                try:
                    analytic_account_local = self.env["account.analytic.account"].browse(
                        int(analytic_account_id_local)
                    )

                    if not analytic_account_local or not analytic_account_local.exists():
                        _logger.warning(
                            "Cuenta analítica origen ID %s no encontrada o no existe.",
                            analytic_account_id_local,
                        )
                        missing_analytics.append(
                            f"ID Local: {analytic_account_id_local} - Cuenta analítica no encontrada en BD origen"
                        )
                        continue

                    analytic_code = analytic_account_local.code
                    analytic_name = analytic_account_local.name

                    # Buscar la cuenta analítica remota (SIN CREAR automáticamente)
                    remote_analytic_id = False

                    # Intentar por código primero
                    if analytic_code:
                        remote_analytic_id = self._find_remote_analytic_by_code(
                            models_proxy, db, uid, password, analytic_code
                        )

                    # Si no encontró por código, intentar por nombre
                    if not remote_analytic_id and analytic_name:
                        remote_analytic_id = self._find_remote_analytic_by_name(
                            models_proxy, db, uid, password, analytic_name
                        )

                    if remote_analytic_id:
                        # ✅ IMPORTANTE: Agregar al dict con ID remoto como KEY y porcentaje como VALUE
                        distribution_dict[str(remote_analytic_id)] = percentage
                        _logger.info(
                            "✅ Cuenta analítica encontrada: %s (Código: %s, Porcentaje: %s%%, ID Origen: %s → ID Remoto: %s)",
                            analytic_name,
                            analytic_code if analytic_code else "SIN CÓDIGO",
                            percentage,
                            analytic_account_id_local,
                            remote_analytic_id,
                        )
                    else:
                        # ❌ Analítica NO encontrada en destino
                        error_detail = (
                            f"Código: {analytic_code if analytic_code else 'SIN CÓDIGO'} - "
                            f"Analítica: '{analytic_name}' (Porcentaje: {percentage}%)"
                        )
                        _logger.error(
                            "❌ Cuenta analítica NO encontrada en BD destino: %s",
                            error_detail,
                        )
                        missing_analytics.append(error_detail)

                except (ValueError, TypeError) as e:
                    _logger.error(
                        "Error procesando analytic_account_id '%s': %s",
                        analytic_account_id_local,
                        str(e),
                    )
                    missing_analytics.append(
                        f"ID Local: {analytic_account_id_local} - Error al procesar (ver logs)"
                    )

            # ❌ LANZAR ERROR si hay analíticas faltantes
            if missing_analytics:
                error_message = (
                    "🚫 **ERROR DE SINCRONIZACIÓN: Cuentas Analíticas NO encontradas en BD destino**\n\n"
                    "No se puede sincronizar esta factura porque las siguientes cuentas analíticas\n"
                    "NO existen en la base de datos destino:\n\n"
                )
                for analytic in missing_analytics:
                    error_message += f"  • {analytic}\n"
                error_message += (
                    "\n**ACCIÓN REQUERIDA:**\n"
                    "Por favor, registre estas cuentas analíticas en la BD destino antes de sincronizar.\n"
                    "Luego reintente enviar el pedido."
                )
                _logger.error(
                    "❌ Error en distribución analítica: %s", error_message)
                raise UserError(_(error_message))

            # Retornar dict con IDs remotos mapeados
            _logger.info(
                "✅ Distribución analítica procesada correctamente: %s",
                distribution_dict,
            )
            return distribution_dict

        except UserError:
            # Relanzar UserError sin capturarlo
            raise
        except Exception as e:
            _logger.error(
                "Error procesando distribución analítica para línea ID %s: %s",
                origin_line.id,
                str(e),
            )
            raise UserError(
                _(f"Error al procesar distribución analítica: {str(e)}")
            )

    def _validate_invoice_analytics_before_send(self):
        """
        Valida que TODAS las cuentas analíticas de la factura origen
        existan en la base de datos destino ANTES de enviar el pedido.

        ✅ OBJETIVO: Bloquear el envío si falta una analítica en destino.
        Esta validación se ejecuta ANTES de crear el pedido en destino.

        Lanza UserError si:
        - Una cuenta analítica en la factura NO existe en destino

        """
        self.ensure_one()

        try:
            models_proxy, db, uid, password = self._get_remote_models_proxy()
        except UserError:
            # Si no puede conectar a destino, relanzar el error
            raise

        # Obtener la factura origen asociada al pedido
        origin_invoices = self.env["account.move"].search(
            [
                ("invoice_origin", "=", self.name),
                ("move_type", "in", ["out_invoice",
                 "in_invoice", "out_refund", "in_refund"]),
            ],
            limit=1,
        )

        if not origin_invoices:
            _logger.info(
                "No se encontró factura original para el pedido '%s'. Validación de analíticas omitida.",
                self.name,
            )
            return True

        origin_invoice = origin_invoices[0]

        # Obtener líneas contables de la factura
        origin_lines = origin_invoice.line_ids.filtered(
            lambda l: l.account_id is not False and l.account_id
        )

        if not origin_lines:
            _logger.info(
                "La factura origen (ID: %s) no tiene líneas contables. Validación omitida.",
                origin_invoice.id,
            )
            return True

        _logger.info(
            "🔍 Validando analíticas de la factura origen (ID: %s) - %d líneas",
            origin_invoice.id,
            len(origin_lines),
        )

        missing_analytics = []

        # Validar cada línea de la factura
        for line_idx, origin_line in enumerate(origin_lines, 1):
            analytic_dist = getattr(origin_line, "analytic_distribution", {})

            if not analytic_dist:
                _logger.debug("Línea %d: Sin distribución analítica", line_idx)
                continue

            # Parsear si es JSON string
            if isinstance(analytic_dist, str):
                try:
                    analytic_dist = json.loads(analytic_dist)
                except (json.JSONDecodeError, TypeError):
                    _logger.warning(
                        "Línea %d: No se pudo parsear analytic_distribution como JSON: %s",
                        line_idx,
                        analytic_dist,
                    )
                    continue

            if not isinstance(analytic_dist, dict):
                _logger.warning(
                    "Línea %d: analytic_distribution no es un diccionario", line_idx)
                continue

            # Validar cada analítica en la distribución
            for analytic_account_id_local, percentage in analytic_dist.items():
                try:
                    analytic_id_int = int(analytic_account_id_local)
                    analytic_account_local = self.env["account.analytic.account"].browse(
                        analytic_id_int
                    )

                    if not analytic_account_local or not analytic_account_local.exists():
                        _logger.warning(
                            "Línea %d: Cuenta analítica ID %s no encontrada en BD origen",
                            line_idx,
                            analytic_account_id_local,
                        )
                        missing_analytics.append(
                            f"ID Local: {analytic_account_id_local} - Cuenta analítica no encontrada en BD origen"
                        )
                        continue

                    analytic_code = analytic_account_local.code
                    analytic_name = analytic_account_local.name

                    # Buscar en destino por código
                    remote_analytic_id = False
                    if analytic_code:
                        remote_analytic_id = self._find_remote_analytic_by_code(
                            models_proxy, db, uid, password, analytic_code
                        )

                    # Si no encontró por código, buscar por nombre
                    if not remote_analytic_id and analytic_name:
                        remote_analytic_id = self._find_remote_analytic_by_name(
                            models_proxy, db, uid, password, analytic_name
                        )

                    if remote_analytic_id:
                        _logger.info(
                            "✅ Línea %d: Analítica '%s' encontrada en BD destino (ID: %s)",
                            line_idx,
                            analytic_name,
                            remote_analytic_id,
                        )
                    else:
                        # ❌ Analítica NO encontrada en destino
                        error_detail = (
                            f"Código: {analytic_code if analytic_code else 'SIN CÓDIGO'} - "
                            f"Analítica: '{analytic_name}' (Porcentaje: {percentage}%)"
                        )
                        _logger.error(
                            "❌ Línea %d: Analítica NO encontrada en BD destino: %s",
                            line_idx,
                            error_detail,
                        )
                        missing_analytics.append(error_detail)

                except (ValueError, TypeError) as e:
                    _logger.error(
                        "Error procesando analytic_account_id '%s': %s",
                        analytic_account_id_local,
                        str(e),
                    )
                    missing_analytics.append(
                        f"ID Local: {analytic_account_id_local} - Error al procesar (ver logs)"
                    )

        # ❌ LANZAR ERROR si hay analíticas faltantes (ANTES de enviar nada)
        if missing_analytics:
            error_message = (
                "🚫 **NO SE PUEDE ENVIAR EL PEDIDO**\n\n"
                "Las siguientes cuentas analíticas de la factura NO existen en la BD destino:\n\n"
            )
            for analytic in missing_analytics:
                error_message += f"  • {analytic}\n"
            error_message += (
                "\n**ACCIÓN REQUERIDA:**\n"
                "Por favor, registre estas cuentas analíticas en la BD destino antes de enviar el pedido.\n"
                "Luego reintente enviar."
            )
            _logger.error(
                "❌ Validación de analíticas bloqueó el envío: %s", error_message)
            raise UserError(_(error_message))

        _logger.info(
            "✅ Validación exitosa: Todas las analíticas de la factura existen en BD destino."
        )
        return True

    def _validate_accounts_for_destination(self):
        """
        Valida que todas las cuentas analíticas del documento origen
        existan en la base de datos destino ANTES de enviar el pedido.

        ✅ OBJETIVO: Evitar enviar pedidos que contengan analíticas
        que no estén registradas en la BD destino.

        Lanza un UserError si:
        - Una cuenta analítica en distribución no existe en destino
        """
        self.ensure_one()

        try:
            models_proxy, db, uid, password = self._get_remote_models_proxy()
        except UserError:
            # Si no puede conectar a destino, relanzar el error
            raise

        # Obtener las líneas del documento (según el modelo)
        if self._name == 'sale.order':
            lines = self.order_line.filtered(lambda l: not l.display_type)
        elif self._name == 'purchase.order':
            lines = self.order_line
        else:
            # Otros modelos que hereden de IntegrationMixin
            lines = getattr(self, 'order_line', [])

        if not lines:
            _logger.info(
                "No hay líneas en el documento. Validación completada.")
            return True

        _logger.info(
            "🔍 Iniciando validación de cuentas analíticas para %d líneas", len(lines))

        missing_analytics = []

        # --- VALIDACIÓN: Cuentas analíticas en distribución ---
        for line_idx, line in enumerate(lines, 1):
            analytic_dist = getattr(line, 'analytic_distribution', {})

            _logger.debug(
                "Línea %d (ID: %s): analytic_distribution = %s",
                line_idx,
                line.id,
                analytic_dist,
            )

            if not analytic_dist:
                _logger.debug("Línea %d: Sin distribución analítica", line_idx)
                continue

            # Parsear si es JSON string
            if isinstance(analytic_dist, str):
                try:
                    analytic_dist = json.loads(analytic_dist)
                    _logger.debug(
                        "Línea %d: analytic_distribution parseado desde JSON", line_idx)
                except (json.JSONDecodeError, TypeError):
                    _logger.warning(
                        "Línea %d: No se pudo parsear analytic_distribution como JSON: %s", line_idx, analytic_dist)
                    continue

            if not isinstance(analytic_dist, dict):
                _logger.warning(
                    "Línea %d: analytic_distribution no es un diccionario", line_idx)
                continue

            # Validar cada analítica en la distribución
            for analytic_account_id_local, percentage in analytic_dist.items():
                try:
                    analytic_id_int = int(analytic_account_id_local)
                    analytic_account_local = self.env["account.analytic.account"].browse(
                        analytic_id_int
                    )

                    if not analytic_account_local or not analytic_account_local.exists():
                        _logger.warning(
                            "Línea %d: Cuenta analítica ID %s no encontrada en BD origen",
                            line_idx,
                            analytic_account_id_local,
                        )
                        missing_analytics.append(
                            f"ID Local: {analytic_account_id_local} - Cuenta analítica no encontrada en BD origen"
                        )
                        continue

                    analytic_code = analytic_account_local.code
                    analytic_name = analytic_account_local.name

                    _logger.info(
                        "Línea %d: Validando analítica '%s' (Código: %s, Porcentaje: %s%%)",
                        line_idx,
                        analytic_name,
                        analytic_code if analytic_code else "SIN CÓDIGO",
                        percentage,
                    )

                    # Buscar en destino por código
                    remote_analytic_id = False
                    if analytic_code:
                        remote_analytic_id = self._find_remote_analytic_by_code(
                            models_proxy, db, uid, password, analytic_code
                        )
                        if remote_analytic_id:
                            _logger.info(
                                "✅ Línea %d: Analítica '%s' encontrada en destino por CÓDIGO (ID Remoto: %s)",
                                line_idx,
                                analytic_name,
                                remote_analytic_id,
                            )

                    # Si no encontró por código, buscar por nombre
                    if not remote_analytic_id and analytic_name:
                        remote_analytic_id = self._find_remote_analytic_by_name(
                            models_proxy, db, uid, password, analytic_name
                        )
                        if remote_analytic_id:
                            _logger.info(
                                "✅ Línea %d: Analítica '%s' encontrada en destino por NOMBRE (ID Remoto: %s)",
                                line_idx,
                                analytic_name,
                                remote_analytic_id,
                            )

                    # Si aún no la encuentra, es un error
                    if not remote_analytic_id:
                        error_detail = (
                            f"Código: {analytic_code if analytic_code else 'SIN CÓDIGO'} - "
                            f"Analítica: '{analytic_name}' (Línea: {line_idx})"
                        )
                        _logger.error(
                            "❌ Línea %d: NO se encontró analítica '%s' en BD destino",
                            line_idx,
                            analytic_name,
                        )
                        missing_analytics.append(error_detail)

                except (ValueError, TypeError) as e:
                    _logger.error(
                        "Error procesando analytic_account_id '%s': %s",
                        analytic_account_id_local,
                        str(e),
                    )
                    missing_analytics.append(
                        f"ID Local: {analytic_account_id_local} - Error al procesar (ver logs)"
                    )

        # --- GENERAR MENSAJE DE ERROR SI FALTAN ANALÍTICAS ---
        if missing_analytics:
            error_message = (
                "🚫 **NO se puede enviar el pedido.**\n\n"
                "Las siguientes cuentas analíticas NO existen en la BD destino.\n"
                "Por favor, registre estas cuentas en la BD destino antes de enviar el pedido.\n\n"
                "**CUENTAS ANALÍTICAS NO ENCONTRADAS:**\n"
            )
            for analytic in missing_analytics:
                error_message += f"  • {analytic}\n"

            _logger.error("❌ Validación fallida: %s", error_message)
            raise UserError(_(error_message))

        _logger.info(
            "✅ Validación completada: Todas las cuentas analíticas existen en BD destino."
        )
        return True

    def _replicate_invoice_accounts(
        self, models_proxy, db, uid, password, remote_invoice_id, invoice_type="out_invoice"
    ):
        """
        Replica los account_id de las líneas de la factura original hacia la factura destino.

        ✅ OBJETIVO: Mantener las mismas cuentas contables en ambas bases de datos.

        Proceso:
        1. Obtener la factura original del local (por invoice_origin = self.name)
        2. Si existe, extraer los account_id de sus líneas
        3. Buscar cada cuenta remota equivalente (por código)
        4. Actualizar las líneas de la factura destino con los account_id remotos correctos

        Args:
            models_proxy: Proxy XML-RPC de la BD destino
            db: Nombre de la BD destino
            uid: ID del usuario autenticado en destino
            password: Contraseña del usuario autenticado
            remote_invoice_id: ID de la factura creada en destino
            invoice_type: Tipo de factura ('out_invoice' o 'in_invoice')

        Returns:
            True si se replicaron exitosamente, False si no hay factura origen
        """
        try:
            # --- PASO 1: Obtener la factura ORIGEN ---
            # Buscamos en el origen por invoice_origin = nombre del pedido actual
            origin_invoices = self.env["account.move"].search(
                [
                    ("invoice_origin", "=", self.name),
                    ("move_type", "in", [
                     "out_invoice", "in_invoice", "out_refund", "in_refund"]),
                ],
                limit=1,
            )

            if not origin_invoices:
                _logger.info(
                    "No se encontró factura original para el pedido '%s'. Saltando replicación de cuentas.",
                    self.name,
                )
                return False

            origin_invoice = origin_invoices[0]
            _logger.info(
                "Factura origen encontrada (ID: %s, Tipo: %s)",
                origin_invoice.id,
                origin_invoice.move_type,
            )

            # --- PASO 2: Extraer account_id de las líneas ORIGEN ---
            # Obtenemos solo las líneas contables (no de sección/comentario)
            origin_lines = origin_invoice.line_ids.filtered(
                lambda l: l.account_id is not False and l.account_id
            )

            if not origin_lines:
                _logger.warning(
                    "La factura origen (ID: %s) no tiene líneas contables válidas.",
                    origin_invoice.id,
                )
                return False

            _logger.info(
                "Se encontraron %d líneas contables en la factura origen.",
                len(origin_lines),
            )

            # --- PASO 3: Obtener líneas de la factura DESTINO ---
            # Las líneas destino se buscan usando el proxy de destino
            remote_lines = models_proxy.execute_kw(
                db,
                uid,
                password,
                "account.move.line",
                "search",
                [
                    [
                        ("move_id", "=", remote_invoice_id),
                        ("account_id", "!=", False),
                    ]
                ],
                {"order": "sequence,id"},  # Orden consistente
            )

            if not remote_lines:
                _logger.warning(
                    "La factura destino (ID: %s) no tiene líneas contables.",
                    remote_invoice_id,
                )
                return False

            _logger.info(
                "Se encontraron %d líneas contables en la factura destino.",
                len(remote_lines),
            )

            # --- PASO 4: Mapear y actualizar cuentas ---
            # Las líneas se deben corresponder en orden (1:1 por índice)
            updates_performed = 0

            for idx, (origin_line, remote_line_id) in enumerate(
                zip(origin_lines, remote_lines)
            ):
                if origin_line.account_id:
                    account_code = origin_line.account_id.code
                    account_name = origin_line.account_id.name

                    # Buscar la cuenta remota equivalente por código
                    remote_account_id = self._find_remote_account_by_code(
                        models_proxy, db, uid, password, account_code
                    )

                    if remote_account_id:
                        # Preparar valores a actualizar
                        update_vals = {"account_id": remote_account_id}

                        # ✅ NUEVA FUNCIONALIDAD: Procesar distribución analítica
                        analytic_distribution = self._process_analytic_distribution(
                            models_proxy, db, uid, password, origin_line
                        )
                        if analytic_distribution:
                            # ✅ CORRECCIÓN: El campo en destino también es 'analytic_distribution'
                            # Formato: {"id_remoto": porcentaje, ...}
                            update_vals["analytic_distribution"] = (
                                analytic_distribution
                            )
                            _logger.info(
                                "Línea %d: Distribución analítica procesada: %s",
                                idx + 1,
                                analytic_distribution,
                            )

                        # Actualizar la línea remota con la cuenta correcta + distribución
                        try:
                            models_proxy.execute_kw(
                                db,
                                uid,
                                password,
                                "account.move.line",
                                "write",
                                [[remote_line_id], update_vals],
                            )
                            _logger.info(
                                "Línea %d: Cuenta actualizada (Código: %s, Nombre: %s, ID Remoto: %s)",
                                idx + 1,
                                account_code,
                                account_name,
                                remote_account_id,
                            )
                            updates_performed += 1
                        except Exception as e:
                            _logger.error(
                                "Error actualizando account_id/distribución en línea remota %s: %s",
                                remote_line_id,
                                str(e),
                            )
                            # No detenemos el proceso por una línea fallida; continuamos
                    else:
                        _logger.warning(
                            "No se encontró cuenta remota equivalente para el código '%s' (Línea origen ID: %s)",
                            account_code,
                            origin_line.id,
                        )

            _logger.info(
                "Replicación de cuentas completada: %d de %d líneas actualizadas.",
                updates_performed,
                len(remote_lines),
            )
            return True

        except UserError:
            # Relanzar UserError (errores de validación de analíticas, etc.)
            raise
        except Exception as e:
            _logger.error(
                "Error durante la replicación de cuentas contables para la factura remota ID %s: %s",
                remote_invoice_id,
                str(e),
            )
            # No lanzamos excepción para no interrumpir el flujo general
            return False
