#!/bin/bash

# CONFIGURACIÓN
FECHA=$(date +"%Y-%m-%d_%H-%M")
PROYECTO="sis_bus"
DESTINO="$HOME/backups/$PROYECTO"
DB="transcap_db"
USER_DB="postgres"

echo "🔄 Iniciando respaldo..."

# Crear carpeta
mkdir -p $DESTINO

# 1. Backup PostgreSQL
echo "📦 Respaldando base de datos..."
pg_dump -U $USER_DB -d $DB > $DESTINO/db_$FECHA.sql

# 2. Backup del proyecto (sin basura)
echo "📁 Respaldando proyecto..."
rsync -av --exclude 'env' --exclude '__pycache__' --exclude '*.pyc' ./ $DESTINO/code_$FECHA

# 3. Comprimir
echo "🗜️ Comprimiendo..."
tar -czf $DESTINO/backup_$FECHA.tar.gz -C $DESTINO code_$FECHA db_$FECHA.sql

# 4. Limpieza (opcional)
rm -rf $DESTINO/code_$FECHA
rm $DESTINO/db_$FECHA.sql

echo "✅ Backup listo en: $DESTINO"
