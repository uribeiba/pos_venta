from django.db import migrations


def create_cejer_bus_layouts(apps, schema_editor):
    BusLayout = apps.get_model("booking", "BusLayout")

    BusLayout.objects.update_or_create(
        name="CEJER 1 Piso",
        defaults={
            "slug": "cejer-1-piso",
            "floors": 1,
            "rows_lower": 12,
            "rows_upper": 0,
            "cols": 4,
            "layout_lower": [],
            "layout_upper": [],
            "numbers_lower": [],
            "numbers_upper": [],
            "prefix_lower": "",
            "prefix_upper": "",
            "background_lower": "img/inferior.png",
            "background_upper": "",
            "editor_config": {
                "lower": {
                    "padding_top": 300,
                    "padding_bottom": 35,
                    "padding_left": 32,
                    "padding_right": 32,
                    "seat_scale": 0.90,
                    "seat_size": 34,
                    "column_gap": 6,
                    "row_gap": 4,
                    "grid_offset_x": 0,
                    "grid_offset_y": 0,
                }
            },
            "structure_config": {},
            "is_active": True,
            "is_system": False,
        },
    )

    BusLayout.objects.update_or_create(
        name="CEJER 2 Pisos",
        defaults={
            "slug": "cejer-2-pisos",
            "floors": 2,
            "rows_lower": 11,
            "rows_upper": 4,
            "cols": 4,
            "layout_lower": [],
            "layout_upper": [],
            "numbers_lower": [],
            "numbers_upper": [],
            "prefix_lower": "",
            "prefix_upper": "",
            "background_lower": "img/inferior.png",
            "background_upper": "img/superior.png",
            "editor_config": {
                "lower": {
                    "padding_top": 300,
                    "padding_bottom": 35,
                    "padding_left": 32,
                    "padding_right": 32,
                    "seat_scale": 0.90,
                    "seat_size": 34,
                    "column_gap": 6,
                    "row_gap": 4,
                    "grid_offset_x": 0,
                    "grid_offset_y": 0,
                },
                "upper": {
                    "padding_top": 95,
                    "padding_bottom": 35,
                    "padding_left": 32,
                    "padding_right": 32,
                    "seat_scale": 0.90,
                    "seat_size": 34,
                    "column_gap": 6,
                    "row_gap": 4,
                    "grid_offset_x": 0,
                    "grid_offset_y": 0,
                },
            },
            "structure_config": {},
            "is_active": True,
            "is_system": False,
        },
    )


def reverse_cejer_bus_layouts(apps, schema_editor):
    # No eliminamos las plantillas al hacer rollback para evitar borrar
    # configuraciones que puedan haber sido utilizadas o ajustadas después.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0060_alter_agency_options_and_more"),
    ]

    operations = [
        migrations.RunPython(
            create_cejer_bus_layouts,
            reverse_cejer_bus_layouts,
        ),
    ]