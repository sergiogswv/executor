
import os
import re

path = r"c:\Users\Sergio\skrymir-suite\executor\app\routes.py"
with open(path, "rb") as f:
    content = f.read()

text = content.decode("utf-8", errors="replace")
text = text.replace('\r\r\n', '\n').replace('\r\n', '\n')

# 1. Limpiar los bloques que quedaron mal
# Buscamos el inicio del loop de validacin y lo reemplazamos COMPLETAMENTE hasta el final de la funcin
# para asegurar coherencia.

# Pero para no romper todo, vamos a ser muy especficos en el bloque del else:

else_block = """                    else:
                        # Build falló
                        logger.warning(f"O Iteracin {iteration_num}: Build FALLO (cdigo={iter_build_exit_code})")

                        # Safeguard 3: Deteccin de regresin de errores
                        _ansi_clean = _re.compile(r'\\x1B[@-_][0-?]*[ -/]*[@-~]')
                        _clean_output = _ansi_clean.sub('', iter_build_output)
                        _error_lines_now = [l for l in _clean_output.splitlines() if "error TS" in l or "error:" in l.lower() or " Error " in l]
                        _new_error_count = len(_error_lines_now)
                        _prev_error_count = len(baseline_errors) if iteration_num == 1 else (
                            len([l for l in _ansi_clean.sub('', iteration_history[-1].get("build_error_preview", "")).splitlines()
                                 if "error TS" in l or "error:" in l.lower()]) if iteration_history else 0
                        )
                        _regression = (_new_error_count > _prev_error_count + 10) and (_new_error_count > len(baseline_errors) + 5)
                        if _regression:
                            logger.error(f"Ys Safeguard: regresin masiva de errores detectada ({_prev_error_count} -> {_new_error_count}). Abortando loop.")

                        iteration_history.append({
                            "iteration": iteration_num,
                            "instruction_preview": current_instruction[:200],
                            "aider_exit_code": exit_code,
                            "files_modified": len(meaningful_files),
                            "build_exit_code": iter_build_exit_code,
                            "build_error_preview": iter_build_output[-5000:],
                            "error_count": _new_error_count,
                            "outcome": "regression_abort" if _regression else "build_failed"
                        })"""

# Reemplazar el bloque else corrupto
text = re.sub(r' +else:\s+# Build fall.*?\n\s+iteration_history\.append\(\{.*?\n\s+"outcome": "regression_abort" if _regression else "build_failed"\n\s+\}\)', else_block, text, flags=re.DOTALL)

with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write(text)

print("Reparacin de bloque else completada.")
