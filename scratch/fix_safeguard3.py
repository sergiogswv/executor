
import os
import re

path = r"c:\Users\Sergio\skrymir-suite\executor\app\routes.py"
with open(path, "rb") as f:
    content = f.read()

text = content.decode("utf-8", errors="replace")
text = text.replace('\r\r\n', '\n').replace('\r\n', '\n')

lines = text.split('\n')
new_lines = []

# Corregir bloque de Safeguard 3 que qued mal indentado
for line in lines:
    if "Safeguard 3" in line and len(line) - len(line.lstrip()) >= 28:
        new_lines.append(line.replace('    ' * 7, '    ' * 6, 1))
    elif "_ansi_clean = _re.compile" in line and len(line) - len(line.lstrip()) >= 28:
        new_lines.append(line.replace('    ' * 7, '    ' * 6, 1))
    elif "_clean_output =" in line and len(line) - len(line.lstrip()) >= 28:
        new_lines.append(line.replace('    ' * 7, '    ' * 6, 1))
    elif "_error_lines_now =" in line and len(line) - len(line.lstrip()) >= 28:
        new_lines.append(line.replace('    ' * 7, '    ' * 6, 1))
    elif "_new_error_count =" in line and len(line) - len(line.lstrip()) >= 28:
        new_lines.append(line.replace('    ' * 7, '    ' * 6, 1))
    elif "_prev_error_count =" in line and len(line) - len(line.lstrip()) >= 28:
        new_lines.append(line.replace('    ' * 7, '    ' * 6, 1))
    elif "_regression =" in line and len(line) - len(line.lstrip()) >= 28:
        new_lines.append(line.replace('    ' * 7, '    ' * 6, 1))
    elif "if _regression:" in line and len(line) - len(line.lstrip()) >= 28:
        new_lines.append(line.replace('    ' * 7, '    ' * 6, 1))
    elif 'logger.error(f"Ys' in line and len(line) - len(line.lstrip()) >= 32:
        new_lines.append(line.replace('    ' * 8, '    ' * 7, 1))
    else:
        new_lines.append(line)

with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write('\n'.join(new_lines))

print("Reparacin de Safeguard 3 completada.")
