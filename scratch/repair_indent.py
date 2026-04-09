
import os
import re

path = r"c:\Users\Sergio\skrymir-suite\executor\app\routes.py"
with open(path, "rb") as f:
    content = f.read()

text = content.decode("utf-8", errors="replace")
text = text.replace('\r\r\n', '\n').replace('\r\n', '\n')

# 1. Encontrar el bloque de validacin y corregir su indentacin
# Buscamos 'if iter_build_exit_code == 0:' y nos aseguramos de que lo que le sigue est bien indentado.

def fix_indentation(text):
    lines = text.split('\n')
    new_lines = []
    in_block = False
    block_indent = 0
    
    for line in lines:
        stripped = line.strip()
        if 'if iter_build_exit_code == 0:' in line:
            # Detectar indentacin base
            base_indent = len(line) - len(line.lstrip())
            new_lines.append(" " * base_indent + "if iter_build_exit_code == 0:")
            in_block = True
            block_indent = base_indent + 4
            continue
            
        if in_block:
            # Si la lnea est vaca, la mantenemos vaca (o con la indentacin del bloque)
            if not stripped:
                new_lines.append("")
                continue
            
            # Si encontramos algo con MENOS indentacin que la base, salimos del bloque
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= (block_indent - 8) and stripped and not stripped.startswith('else:'):
                in_block = False
            
        if in_block:
            # Forzar indentacin mnima para el bloque
            # Pero respetando la estructura interna (if/else/etc)
            current_indent = len(line) - len(line.lstrip())
            # Intentar adivinar la indentacin relativa
            # Si la lnea original tena poca indentacin (ej. por errores previos), la forzamos
            if current_indent < block_indent:
                # Si es un 'else:' o 'elif:', debe estar al nivel del if anterior (block_indent)
                # pero aqu estamos ya dentro del bloque de 'if iter_build_exit_code == 0:'
                new_lines.append(" " * block_indent + stripped)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
            
    return '\n'.join(new_lines)

# Aplicar una limpieza ms agresiva del bloque corrupto antes de re-indentar
# Eliminamos duplicados conocidos
text = text.replace('if test_cmd and options.get("run_tests", test_cmd_explicit):\n                            if test_cmd and options.get("run_tests", test_cmd_explicit):', 'if test_cmd and options.get("run_tests", test_cmd_explicit):')

# Re-aplicar la lgica de test file-specific con la indentacin correcta
# Suponemos base_indent = 20 (standard para este archivo en ese loop)
final_logic = """                    if iter_build_exit_code == 0:
                        # Build exitoso... corremos tests?
                        if test_cmd and options.get("run_tests", test_cmd_explicit):
                            # --- Determinar comando de test especfico ---
                            specific_tests = _find_tests_for_files(meaningful_files, project_path)
                            current_test_cmd = list(test_cmd)
                            if specific_tests:
                                logger.info(f"Y Archivos de test detectados: {specific_tests}")
                                if build_tool == "nodejs":
                                    if current_test_cmd == ["npm", "test"]:
                                        current_test_cmd += ["--"]
                                    current_test_cmd += specific_tests
                                elif build_tool == "python":
                                    current_test_cmd += specific_tests
                            
                            logger.info(f"Y [{build_tool}] Build OK. Ejecutando tests: {' '.join(current_test_cmd)}")
                            test_svc = ServiceDefinition(
                                name=f"test_{build_tool}",
                                command=current_test_cmd[0],
                                cwd=project_path,
                                env=auto_fix_env,
                                shell=True
                            )
                            test_result = await _manager.run_once(
                                f"test_{build_tool}", test_svc,
                                command_list=current_test_cmd, timeout=180
                            )
                            iter_test_exit_code = test_result.get("exit_code", -1)
                            iter_test_output = (test_result.get("stdout") or "") + (test_result.get("stderr") or "")
                            
                            if iter_test_exit_code == 0:
                                fix_validated = True
                                logger.info(f"o. Iteracin {iteration_num}: Tests EXITOSOS en rama {branch_name}")
                                iteration_history.append({
                                    "iteration": iteration_num,
                                    "instruction_preview": current_instruction[:200],
                                    "aider_exit_code": exit_code,
                                    "files_modified": len(meaningful_files),
                                    "build_exit_code": 0,
                                    "test_exit_code": 0,
                                    "outcome": "success"
                                })
                                break
                            else:
                                _no_tests_indicators = ["collected 0 items", "No tests found", "no tests were found", "0 tests passed"]
                                if any(ind.lower() in iter_test_output.lower() for ind in _no_tests_indicators):
                                    logger.info(f"o. Iteracin {iteration_num}: No se encontraron tests. Continuando.")
                                    fix_validated = True
                                    iteration_history.append({
                                        "iteration": iteration_num,
                                        "files_modified": len(meaningful_files),
                                        "build_exit_code": 0,
                                        "outcome": "success_no_tests"
                                    })
                                    break
                                
                                logger.warning(f"O Iteracin {iteration_num}: Tests FALLARON (cdigo={iter_test_exit_code})")
                                iter_build_exit_code = iter_test_exit_code
                                iter_build_output = iter_test_output
                        else:
                            fix_validated = True
                            logger.info(f"o. Iteracin {iteration_num}: Build EXITOSO (sin tests) en rama {branch_name}")
                            iteration_history.append({
                                "iteration": iteration_num,
                                "files_modified": len(meaningful_files),
                                "build_exit_code": 0,
                                "outcome": "success"
                            })
                            break
                    else:
                        # Build fall
                        logger.warning(f"O Iteracin {iteration_num}: Build FALLO (cdigo={iter_build_exit_code})")
"""

# Reemplazar el bloque entero desde 'if iter_build_exit_code == 0:' hasta el final del bloque de validacin.
# Usamos un regex que atrape el bloque problematico.
# Buscamos desde 'if iter_build_exit_code == 0:' hasta la mencin de 'Safeguard 3'
text = re.sub(r'\s*if iter_build_exit_code == 0:.*?Safeguard 3', '\n' + final_logic + '\n                            # Safeguard 3', text, flags=re.DOTALL)

with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write(text)

print("Reparacin de indentacin completada.")
