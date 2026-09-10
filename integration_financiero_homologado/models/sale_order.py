# /integration_financiero_homologado/models/sale_order.py
import logging
from odoo import models, fields, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _name = 'sale.order'
    _inherit = ['sale.order', 'integration.mixin']

    homologado_invoice_id = fields.Integer(
        string="ID Factura Destino",
        readonly=True,
        copy=False
    )

    def _prepare_homologado_sale_data(self):
        """
        Prepara el diccionario de valores para enviar la Venta.
        Busca la tarifa en destino que maneje USD y envía el price_unit en esa moneda.
        """
        models_proxy, db, uid, password = self._get_remote_models_proxy()

        # Validación mínima local
        partner_identifier = self.partner_id.vat or getattr(self.partner_id, "rif", False) or getattr(self.partner_id, "identification_id", False)
        if not partner_identifier:
            raise UserError(_("El cliente '%s' no tiene RIF/C.I/VAT configurado.") % self.partner_id.name)

        # Busca y si no existe, crea partner remoto
        partner_id_remoto = self._get_or_create_remote_partner(
            models_proxy, db, uid, password, self.partner_id
        )

        # Usuario fijo configurado para crear documentos en destino
        user_id_remoto = self._get_fixed_remote_user_id(
            models_proxy, db, uid, password
        )

        # Obtener campos remotos del modelo de línea para filtrar vals no soportados
        line_remote_fields = self._remote_fields(
            models_proxy, db, uid, password, 'sale.order.line'
        )

        # Búsqueda del término de pago remoto
        payment_term_id_remoto = False
        if self.payment_term_id:
            try:
                domain = [('name', '=', self.payment_term_id.name)]
                payment_terms = models_proxy.execute_kw(
                    db, uid, password, 'account.payment.term', 'search',
                    [domain], {'limit': 1}
                )
                if payment_terms:
                    payment_term_id_remoto = payment_terms[0]
            except Exception as e:
                _logger.warning(f"⚠️ Error buscando término de pago remoto: {e}")

        # ✅ NUEVO: Buscar Lista de Precios USD Remota por el campo currency_id
        pricelist_id_remoto = False
        if self.currency_id.name == 'USD':
            try:
                # Usamos notación de punto para buscar la tarifa cuya moneda se llame 'USD'
                domain_pricelist = [('currency_id.name', '=', 'USD')]
                pricelists = models_proxy.execute_kw(
                    db, uid, password, 'product.pricelist', 'search',
                    [domain_pricelist], {'limit': 1}
                )
                if pricelists:
                    pricelist_id_remoto = pricelists[0]
                    _logger.info(f"✅ Tarifa con divisa USD encontrada en destino: ID {pricelist_id_remoto}")
                else:
                    _logger.warning(f"⚠️ Tarifa con divisa USD NO encontrada en destino. Odoo calculará el pedido en la moneda por defecto.")
            except Exception as e:
                _logger.warning(f"⚠️ Error buscando tarifa remota por divisa: {e}")

        order_lines = []
        for line in self.order_line.filtered(lambda l: not l.display_type):
            # Busca y si no existe, crea producto remoto. Pasamos la UoM de la línea
            # para que el template remoto use esa unidad si es necesario.
            product_id_remoto = self._get_or_create_remote_product(
                models_proxy, db, uid, password, line.product_id, line.product_uom
            )
            # Asegurar la UoM remota para la línea y enviarla
            uom_remote_id = False
            if getattr(line, 'product_uom', False):
                try:
                    uom_remote_id = self._get_or_create_remote_uom(
                        models_proxy, db, uid, password, line.product_uom
                    )
                except Exception:
                    uom_remote_id = False
            
            line_vals = {}
            if product_id_remoto:
                line_vals['product_id'] = product_id_remoto
            
            if line.product_uom_qty:
                line_vals['product_uom_qty'] = line.product_uom_qty
            if uom_remote_id:
                line_vals['product_uom'] = uom_remote_id
            
            # Enfoque USD: Enviar price_unit directamente a price_unit en destino
            if self.currency_id.name == 'USD':
                line_vals['price_unit'] = line.price_unit
                
                # Limpieza de subtotales y campos que puedan generar conflictos
                campos_a_quitar = ['price_subtotal', 'subtotal_usd', 'ref_subtotal', 'price_unit_bs', 'ref_unit']
                for field in campos_a_quitar:
                    if field in line_vals:
                        del line_vals[field]
            else:
                # Comportamiento estándar para moneda nacional u otras
                if line.price_unit:
                    line_vals['price_unit'] = line.price_unit

            # Mapear impuestos de la línea
            try:
                taxes = getattr(line, 'tax_id', False)
                remote_tax_ids = []
                if taxes:
                    remote_tax_ids = self._map_remote_taxes(
                        models_proxy, db, uid, password, taxes, usage='sale'
                    )

                if remote_tax_ids and 'tax_id' in line_remote_fields:
                    line_vals['tax_id'] = [(6, 0, remote_tax_ids)]
            except Exception as e:
                pass

            # Filtrar valores no soportados por el destino
            clean_line_vals = self._filter_remote_vals(line_vals, line_remote_fields)
            order_lines.append((0, 0, clean_line_vals))

        # Construir el diccionario final de la orden
        return_vals = {
            'date_order': fields.Datetime.to_string(self.date_order) if self.date_order else fields.Datetime.now(),
            'origin': self.name or 'SIN_NOMBRE',
            'order_line': order_lines,
            'partner_id': partner_id_remoto,
            'user_id': user_id_remoto,
            
            # Identificador para la BD receptora
            'from_integration_emisora': True,
        }
        
        # Establecer la tarifa USD encontrada en la cabecera
        if pricelist_id_remoto:
            return_vals['pricelist_id'] = pricelist_id_remoto

        if payment_term_id_remoto:
            return_vals['payment_term_id'] = payment_term_id_remoto
        
        return return_vals

    def action_send_to_homologado(self):
        """Prepara los datos y llama al método genérico con las acciones de Venta."""
        self.ensure_one()
        if self.homologado_id:
            raise UserError(
                _("Este pedido ya fue enviado a la BD destino (ID: %s).")
                % self.homologado_id
            )

        # Validación de analíticas antes de enviar
        if hasattr(self, '_validate_invoice_analytics_before_send'):
            self._validate_invoice_analytics_before_send()

        vals = self._prepare_homologado_sale_data()
        return self._action_send_to_homologado_generic(
            remote_model='sale.order',
            vals=vals,
            confirm_method='action_confirm',
            invoice_method='action_create_invoice_wizard'
        )