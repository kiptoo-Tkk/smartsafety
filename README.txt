SETUP
Pi:    pip install flask pyserial psutil
       sudo usermod -aG dialout $USER   (then log out/in)
       copy templates/ and static/ files INTO your existing project (keep your dashboard.css, common.js,
       chart.umd.min.js, fontawesome). Delete old templates/routes. Run: python3 app.py  ->  http://<pi-ip>:5000
ESP32: open esp32_motor_node.ino, set a UNIQUE MOTOR_ID per board (motor_1, motor_2 ...), flash, plug into a POWERED USB hub on the Pi.
