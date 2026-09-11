# /integration_financiero_homologado/models/purchase_order.py
import logging
from odoo import models, fields, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _name = 'purchase.order'
    _inherit = ['purchase.order', 'integration.mixin']

    homologado_invoice_id = fields.Integer(
        string="ID Factura Destino",
        readonly=True,
        copy=False
    )

    def _prepare_homologado_purchase_data(self):
        """Prepara el diccionario de valores para enviar la Compra."""
        models_proxy, db, uid, password = self._get_remote_models_proxy()

        # Validación mínima local
        partner_identifier = self.partner_id.vat or getattr(self.partner_id, "rif", False) or getattr(self.partner_id, "identification_id", False)
        if not partner_identifier:
            raise UserError(_("El proveedor '%s' no tiene RIF/C.I/VAT configurado.") % self.partner_id.name)

        # ✅ AHORA: busca y si no existe, crea partner remoto
        partner_id_remoto = self._get_or_create_remote_partner(
            models_proxy, db, uid, password, self.partner_id
        )

        # Usuario fijo configurado para crear documentos en destino
        user_id_remoto = self._get_fixed_remote_user_id(
            models_proxy, db, uid, password
        )

        # Obtener campos remotos de orden y línea para filtrar vals no soportados
        order_remote_fields = self._remote_fields(
            models_proxy, db, uid, password, 'purchase.order'
        )
        line_remote_fields = self._remote_fields(
            models_proxy, db, uid, password, 'purchase.order.line'
        )

        # Búsqueda de moneda remota (ej. USD)
        currency_id_remoto = False
        if self.currency_id:
            try:
                curr_ids = models_proxy.execute_kw(
                    db, uid, password, 'res.currency', 'search',
                    [[('name', '=', self.currency_id.name)]], {'limit': 1}
                )
                if curr_ids:
                    currency_id_remoto = curr_ids[0]
                    _logger.info("Moneda remota '%s' encontrada: ID %s", self.currency_id.name, currency_id_remoto)
            except Exception as e:
                _logger.warning("Error buscando moneda remota '%s': %s", self.currency_id.name, e)

        # ✅ NUEVO: Obtener el ID remoto del término de pago (si existe)
        payment_term_id_remoto = False
        if self.payment_term_id:
            try:
                payment_terms = models_proxy.execute_kw(
                    db, uid, password, 'account.payment.term', 'search',
                    [['name', '=', self.payment_term_id.name]], {'limit': 1}
                )
                if payment_terms:
                    payment_term_id_remoto = payment_terms[0]
            except Exception as e:
                _logger.warning(f"Error buscando término de pago remoto: {e}")

        order_lines = []
        for line in self.order_line:
            # ✅ AHORA: busca y si no existe, crea producto remoto
            line_price_usd = (
                line.price_unit
                if self.currency_id.name == 'USD'
                else getattr(line, 'ref_unit', False) or line.price_unit
            )
            product_id_remoto = self._get_or_create_remote_product(
                models_proxy,
                db,
                uid,
                password,
                line.product_id,
                line_uom=getattr(line, 'product_uom', None),
                default_price=line_price_usd,
            )

            line_vals = {
                'product_id': product_id_remoto,
                'name': line.name,
                'product_qty': line.product_qty,
                'date_planned': line.date_planned.strftime('%Y-%m-%d %H:%M:%S') if line.date_planned else False,
            }

            if self.currency_id.name == 'USD':
                # Enviar precio en USD como price_unit
                line_vals['price_unit'] = line.price_unit
                if 'ref_unit' in line_remote_fields:
                    line_vals['ref_unit'] = line.price_unit
            else:
                # Comportamiento normal para otras monedas
                line_vals['price_unit'] = line.price_unit


            # Mapear impuestos de la línea hacia IDs remotos (específico para compras)
            try:
                taxes = getattr(line, 'taxes_id', False)
                remote_tax_ids = []
                if taxes:
                    remote_tax_ids = self._map_remote_taxes(
                        models_proxy, db, uid, password, taxes, usage='purchase'
                    )

                if remote_tax_ids:
                    if 'taxes_id' in line_remote_fields:
                        line_vals['taxes_id'] = [(6, 0, remote_tax_ids)]
                    else:
                        _logger.warning(
                            "El modelo remoto de línea de compra no expone 'taxes_id'; impuestos no enviados"
                        )
            except Exception as e:
                # No detener el proceso por un fallo de mapeo de impuestos; avisar en logs
                _logger.warning('No se pudo mapear impuestos de la línea: %s', e)

            clean_line_vals = self._filter_remote_vals(line_vals, line_remote_fields)

            order_lines.append((0, 0, clean_line_vals))

        # ✅ Construir return de forma segura sin valores None
        return_vals = {
            'date_order': fields.Datetime.to_string(self.date_order) if self.date_order else fields.Datetime.now(),
            'origin': self.name or 'SIN_NOMBRE',
            'order_line': order_lines,
        }
        
        # Agregar partner_id solo si existe
        if partner_id_remoto:
            return_vals['partner_id'] = partner_id_remoto
        
        # Agregar user_id solo si existe  
        if user_id_remoto:
            return_vals['user_id'] = user_id_remoto
        
        # ✅ Agregar payment_term_id para cálculo correcto de date_maturity
        if payment_term_id_remoto:
            return_vals['payment_term_id'] = payment_term_id_remoto

        # ✅ Agregar moneda remota para mantener la compra en la divisa original (ej. USD)
        if currency_id_remoto and 'currency_id' in order_remote_fields:
            return_vals['currency_id'] = currency_id_remoto

        return self._filter_remote_vals(return_vals, order_remote_fields)

    def action_send_to_homologado(self):
        """Prepara los datos y llama al método genérico con las acciones de Compra."""
        self.ensure_one()
        if self.homologado_id:
            raise UserError(
                _("Esta orden de compra ya fue enviada a la BD destino (ID: %s).")
                % self.homologado_id
            )

        # ✅ VALIDACIÓN CRÍTICA: Verificar analíticas de la factura origen ANTES de enviar el pedido
        # Si falta una analítica en destino, esto lanzará un error y NO se enviará nada
        self._validate_invoice_analytics_before_send()

        vals = self._prepare_homologado_purchase_data()
        return self._action_send_to_homologado_generic(
            remote_model='purchase.order',
            vals=vals,
            confirm_method='button_confirm',
            invoice_method='action_create_invoice'
        )
