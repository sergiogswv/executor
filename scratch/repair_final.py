
import os
import re

path = r"c:\Users\Sergio\skrymir-suite\executor\app\routes.py"
with open(path, "rb") as f:
    content = f.read()

text = content.decode("utf-8", errors="replace")
text = text.replace('\r\r\n', '\n').replace('\r\n', '\n')

# Reparo la lgica con la indentacin exacta de 20 espacios para los bloques principales (if/else)
# y 24 espacios para el contenido interno.

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
                        # Build falló
                        logger.warning(f"O Iteracin {iteration_num}: Build FALLO (cdigo={iter_build_exit_code})")"""

# El Reemplazo: Buscamos el bloque desde 'if iter_build_exit_code == 0' hasta el comentario de Safeguard 3.
# Limpiamos los espacios extras en el comentario de destino.
text = re.sub(r' +if iter_build_exit_code == 0:.*?# Safeguard 3', final_logic + '\n\n                        # Safeguard 3', text, flags=re.DOTALL)

with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write(text)

print("Reparacin de indentacin final completada.")
