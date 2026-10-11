"""The Qt control surface: tray icon, notifications, floating controller and wiring.

Everything here may import ``PyQt6`` but must never import ``onnxruntime``; the engine
must stay headless so ``utter speak`` works without a GUI (spec §4.1).
"""
