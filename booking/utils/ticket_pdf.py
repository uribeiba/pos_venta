# booking/utils/ticket_pdf.py
"""
Generador de PDF de boletos con:
- Formato A4, 2 boletos por página.
- Código QR con payload firmado (HMAC) para verificación.
- Línea de corte punteada entre el boleto del pasajero y la copia tripulación.
- Diseño inspirado en TurBus / PlussChile.
"""
from __future__ import annotations

import io
from decimal import Decimal

import qrcode
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from django.conf import settings
from django.core import signing
from django.utils import timezone


# =============================================================================
# CONFIGURACIÓN
# =============================================================================
PRIMARY = HexColor('#0f172a')      # Azul oscuro corporativo
ACCENT = HexColor('#f5821f')       # Naranjo
DARK_GRAY = HexColor('#64748b')
LIGHT_GRAY = HexColor('#e2e8f0')
BG_GRAY = HexColor('#f8fafc')
RED = HexColor('#dc2626')


LEGAL_TEXT = (
    "Válido para la fecha y hora señalada. La devolución o cambio se aceptará hasta 4 horas "
    "antes del viaje (art. 67 D.S. 212/92 MT). Si canceló en efectivo, con tarjeta de crédito, "
    "débito u otra, se devolverá el 85% del valor. Si el valor del equipaje excede 5 UTM es su "
    "obligación declararlo previamente en la oficina de origen (art. 70 D.S. 212/92 MT). La empresa "
    "no se hace responsable por pérdidas al interior del bus. El horario de llegada es estimado. "
    "No se aceptarán boletos rotos o adulterados. Al abordar, presente este boleto (impreso o digital) "
    "junto a su cédula de identidad."
)


class TicketPDFGenerator:
    """Genera un PDF con boletos electrónicos listos para imprimir."""

    PAGE_W, PAGE_H = A4                 # 210 x 297 mm
    MARGIN_X = 10 * mm
    MARGIN_Y = 8 * mm
    TICKET_H = (PAGE_H - 2 * MARGIN_Y) / 2   # 2 boletos por página
    TICKET_W = PAGE_W - 2 * MARGIN_X

    def __init__(self):
        self.company_name = getattr(settings, 'COMPANY_NAME', 'Cejer')
        self.company_phone = getattr(settings, 'COMPANY_PHONE', '600 000 0000')
        self.company_web = getattr(settings, 'COMPANY_WEB', 'www.cejer.cl')

    # ------------------------------------------------------------------ public
    def generate(self, tickets) -> bytes:
        """Retorna los bytes del PDF con todos los boletos (2 por página)."""
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        c.setTitle("Boletos electrónicos")

        tickets = list(tickets)
        for idx, ticket in enumerate(tickets):
            position = idx % 2          # 0 = top, 1 = bottom
            if idx > 0 and position == 0:
                c.showPage()
            y = self.MARGIN_Y + (1 - position) * self.TICKET_H
            self._draw_ticket(c, ticket, self.MARGIN_X, y, self.TICKET_W, self.TICKET_H)

        c.save()
        buf.seek(0)
        return buf.getvalue()

    # --------------------------------------------------------------- internals
    def _draw_ticket(self, c, ticket, x, y, w, h):
        """
        Estructura interna de un boleto (h ~ 140 mm):
          - Boleto pasajero ........... 62% de la altura
          - Línea de corte ............ 3 mm
          - Copia tripulación ......... resto
        """
        main_h = h * 0.62
        cut_h = 4 * mm
        crew_h = h - main_h - cut_h

        # ---------- Boleto del pasajero ----------
        self._draw_main(c, ticket, x, y + crew_h + cut_h, w, main_h)

        # ---------- Línea de corte ----------
        cut_y = y + crew_h + cut_h / 2
        c.saveState()
        c.setStrokeColor(DARK_GRAY)
        c.setLineWidth(0.4)
        c.setDash(2, 3)
        c.line(x + 4 * mm, cut_y, x + w - 4 * mm, cut_y)
        c.restoreState()

        # ---------- Texto "cortar aquí" ----------
        c.saveState()
        c.setFont('Helvetica', 6)
        c.setFillColor(DARK_GRAY)
        c.drawCentredString(x + w / 2, cut_y + 1, "✂  - - - - - - - - - - - - - -  CORTAR AQUÍ  - - - - - - - - - - - - - -  ✂")
        c.restoreState()

        # ---------- Copia tripulación ----------
        self._draw_crew(c, ticket, x, y, w, crew_h)

    # ---------------------------------------------------------------- main part
    def _draw_main(self, c, ticket, x, y, w, h):
        # Header
        header_h = 11 * mm
        c.setFillColor(PRIMARY)
        c.rect(x, y + h - header_h, w, header_h, fill=1, stroke=0)

        c.setFillColor(white)
        c.setFont('Helvetica-Bold', 14)
        c.drawString(x + 4 * mm, y + h - 7 * mm, self.company_name.upper())

        c.setFont('Helvetica', 6.5)
        c.drawRightString(x + w - 4 * mm, y + h - 5 * mm, f"Call Center: {self.company_phone}")
        c.drawRightString(x + w - 4 * mm, y + h - 8 * mm, self.company_web)

        # Columnas
        left_x = x + 4 * mm
        left_w = w * 0.65
        qr_x = x + w - 4 * mm - 30 * mm
        qr_size = 30 * mm
        body_top = y + h - header_h - 4 * mm

        # ---------- Fila: RUT + Nombre ----------
        c.setFillColor(black)
        c.setFont('Helvetica-Bold', 8)
        c.drawString(left_x, body_top, "RUT:")
        c.setFont('Helvetica', 9)
        c.drawString(left_x + 12 * mm, body_top, ticket.national_id or "—")

        c.setFont('Helvetica-Bold', 8)
        c.drawString(left_x + 45 * mm, body_top, "PASAJERO:")
        c.setFont('Helvetica-Bold', 9)
        c.drawString(left_x + 68 * mm, body_top, (ticket.buyer_name or "Pasajero").upper()[:28])

        # ---------- Fila: Boleto + Asiento ----------
        row_y = body_top - 6 * mm
        c.setFont('Helvetica-Bold', 8)
        c.drawString(left_x, row_y, "BOLETO:")
        c.setFont('Helvetica-Bold', 11)
        c.setFillColor(ACCENT)
        c.drawString(left_x + 18 * mm, row_y, ticket.number)

        c.setFillColor(black)
        c.setFont('Helvetica-Bold', 8)
        c.drawString(left_x + 50 * mm, row_y, "ASIENTO:")
        c.setFont('Helvetica-Bold', 14)
        c.drawString(left_x + 68 * mm, row_y, str(ticket.seat.number))
        c.setFont('Helvetica', 7)
        c.drawString(left_x + 80 * mm, row_y + 3, f"Piso {ticket.seat.deck}")

        # ---------- Fila: Origen → Destino ----------
        row_y -= 7 * mm
        c.setFont('Helvetica-Bold', 8)
        c.setFillColor(black)
        c.drawString(left_x, row_y, "ORIGEN:")
        c.setFont('Helvetica', 9)
        c.drawString(left_x + 18 * mm, row_y, ticket.trip.route.origin.name)

        c.setFont('Helvetica-Bold', 8)
        c.drawString(left_x + 50 * mm, row_y, "DESTINO:")
        c.setFont('Helvetica', 9)
        c.drawString(left_x + 68 * mm, row_y, ticket.trip.route.destination.name)

        # ---------- Fila: Fecha / Hora ----------
        row_y -= 6 * mm
        dep = timezone.localtime(ticket.trip.departure)
        c.setFont('Helvetica-Bold', 8)
        c.drawString(left_x, row_y, "FECHA:")
        c.setFont('Helvetica-Bold', 10)
        c.drawString(left_x + 18 * mm, row_y, dep.strftime("%d/%m/%Y"))
        c.setFont('Helvetica-Bold', 8)
        c.drawString(left_x + 50 * mm, row_y, "HORA:")
        c.setFont('Helvetica-Bold', 10)
        c.drawString(left_x + 68 * mm, row_y, dep.strftime("%H:%M"))

        # ---------- Fila: Servicio + Precio ----------
        row_y -= 6 * mm
        service = (ticket.trip.bus.service_type or 'semi_cama').replace('_', ' ').title()
        c.setFont('Helvetica-Bold', 8)
        c.drawString(left_x, row_y, "SERVICIO:")
        c.setFont('Helvetica', 9)
        c.drawString(left_x + 18 * mm, row_y, service)

        c.setFont('Helvetica-Bold', 8)
        c.drawString(left_x + 50 * mm, row_y, "PRECIO:")
        c.setFont('Helvetica-Bold', 11)
        c.setFillColor(PRIMARY)
        c.drawString(left_x + 68 * mm, row_y, f"${int(ticket.price):,}".replace(",", "."))

        # ---------- QR ----------
        qr_img = self._qr_for_ticket(ticket)
        c.setFillColor(black)
        c.drawImage(qr_img, qr_x, y + 22 * mm, width=qr_size, height=qr_size)

        c.setFont('Helvetica', 5.5)
        c.setFillColor(DARK_GRAY)
        c.drawCentredString(qr_x + qr_size / 2, y + 20 * mm, f"Cód. verificación: {ticket.number}")

        # ---------- Texto legal (parte inferior) ----------
        self._draw_wrapped_text(
            c, LEGAL_TEXT,
            x + 4 * mm, y + 3 * mm,
            w - 8 * mm, font_size=4.8, line_height=5.4,
        )

    # -------------------------------------------------------------- crew copy
    def _draw_crew(self, c, ticket, x, y, w, h):
        # Fondo claro
        c.setFillColor(BG_GRAY)
        c.rect(x, y, w, h, fill=1, stroke=0)

        # Borde
        c.setStrokeColor(LIGHT_GRAY)
        c.setLineWidth(0.4)
        c.rect(x, y, w, h, fill=0, stroke=1)

        # Título
        c.setFillColor(PRIMARY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(x + 4 * mm, y + h - 6 * mm, "COPIA TRIPULACIÓN")

        # QR pequeño
        qr_img = self._qr_for_ticket(ticket)
        qr_size = 20 * mm
        c.drawImage(qr_img, x + w - qr_size - 4 * mm, y + 3 * mm, width=qr_size, height=qr_size)

        # Columnas de datos
        col1 = x + 4 * mm
        col2 = x + w * 0.42

        def line(yy, label, value, bold_value=True):
            c.setFillColor(DARK_GRAY)
            c.setFont('Helvetica-Bold', 6.5)
            c.drawString(col1 if col == 1 else col2, yy, label)
            c.setFillColor(black)
            c.setFont('Helvetica-Bold' if bold_value else 'Helvetica', 8)
            c.drawString((col1 if col == 1 else col2) + 18 * mm, yy, str(value)[:40])

        row = y + h - 13 * mm

        # Columna 1
        col = 1
        line(row, "BOLETO:", ticket.number); row -= 4 * mm
        line(row, "PASAJERO:", (ticket.buyer_name or "")[:30]); row -= 4 * mm
        line(row, "RUT:", ticket.national_id or "—"); row -= 4 * mm

        # Columna 2
        col = 2
        row2 = y + h - 13 * mm
        line(row2, "ASIENTO:", f"{ticket.seat.number} (Piso {ticket.seat.deck})"); row2 -= 4 * mm
        dep = timezone.localtime(ticket.trip.departure)
        line(row2, "FECHA:", dep.strftime("%d/%m/%Y %H:%M")); row2 -= 4 * mm
        line(row2, "RUTA:", f"{ticket.trip.route.origin.name} → {ticket.trip.route.destination.name}",
             bold_value=False)

    # ------------------------------------------------------------------- QR
    def _qr_for_ticket(self, ticket) -> ImageReader:
        """QR firmado con HMAC que permite verificar sin BD."""
        payload = {
            'tn': ticket.number,
            'tid': ticket.trip_id,
            'seat': f"{ticket.seat.deck}-{ticket.seat.number}",
            'dep': ticket.trip.departure.isoformat(),
            'rut': ticket.national_id or '',
        }
        signed = signing.dumps(payload, salt='ticket-verify', compress=True)
        verify_url = getattr(settings, 'TICKET_VERIFY_URL', 'https://midominio.cl/boletos/verify/')
        qr_data = f"{verify_url}?t={signed}"

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6,
            border=2,
        )
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return ImageReader(buf)

    # -------------------------------------------------------------- helpers
    def _draw_wrapped_text(self, c, text, x, y, max_width, font_size=6, line_height=6):
        """Dibuja texto con salto de línea, de abajo hacia arriba."""
        c.setFont('Helvetica', font_size)
        c.setFillColor(DARK_GRAY)

        words = text.split()
        lines, current = [], ""
        for word in words:
            test = (current + " " + word).strip()
            if c.stringWidth(test, 'Helvetica', font_size) <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)

        # De abajo hacia arriba
        for i, ln in enumerate(lines):
            c.drawString(x, y + i * line_height, ln)


# =============================================================================
# Función de conveniencia
# =============================================================================
def generate_ticket_pdf(tickets) -> bytes:
    """Genera el PDF con los boletos dados."""
    if not tickets:
        return b""
    return TicketPDFGenerator().generate(tickets)