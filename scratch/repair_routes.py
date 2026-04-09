
import os
import re

path = r"c:\Users\Sergio\skrymir-suite\executor\app\routes.py"
with open(path, "rb") as f:
    content = f.read()

# Intentar decodear como utf-8
text = content.decode("utf-8", errors="replace")

# 1. Corregir dobles saltos de lnea (\r\r\n -> \r\n o \n\n -> \n si fue error de escritura)
# Si el archivo tiene \r\r\n, lo normalizamos a \n
text = text.replace('\r\r\n', '\n').replace('\r\n', '\n')

# 2. Corregir el error especfico de la lnea de continuacin \
# "elif ... or \ \n \n os.path" -> "elif ... or \ \n os.path"
text = re.sub(r'or\s*\\\s*\n\s*\n', 'or \\\n', text)

# 3. Asegurarse de que no haya otros \ seguidos de lneas vacas
text = re.sub(r'\\\s*\n\s*\n', '\\\n', text)

# 4. Verificar si hay duplicacin del helper _find_tests_for_files
# Si hay mltiples definiciones, dejar solo una.
if text.count('def _find_tests_for_files') > 1:
    # Mantener solo la primera ocurrencia del bloque de la funcin
    parts = text.split('def _find_tests_for_files')
    text = parts[0] + 'def _find_tests_for_files' + parts[1] + "".join(p for p in parts[2:] if "Intenta encontrar archivos" not in p)

with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write(text)

print("Reparacin completada.")
