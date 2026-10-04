# llama як роль service-roles (ПРОПОЗИЦІЯ, нічого не встановлено)

Факти з хоста (прочитано, 2026-10-04): llama — КОРИСТУВАЦЬКИЙ сервіс
`~/.config/systemd/user/llama.service`, exe `/var/home/keefeere/.local/bin/llama`
(мітка `gconf_home_t`), cgroup
`/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/llama.service`,
головний процес і worker мають однаковий exe і cgroup, uid 1000.

## Обмеження дизайну
`ReEnrollRole` — root-only D-Bus. Користувацький unit (`ExecStartPre=` без root) його викликати не може,
`ExecStartPre=+` у user-менеджері НЕ дає root. Тому enrollment після рестарту llama потребує
root-ініційованого кроку. Варіанти:
1. Запускати llama як СИСТЕМНИЙ сервіс `User=keefeere` + `ExecStartPre=+/usr/bin/python3
   /path/egpu-service-roles-ctl.py enroll 0 llama.service --exe /var/home/keefeere/.local/bin/llama`
   (cgroup створюється systemd до ExecStartPre; перший exec llama тоді вже допускається).
2. Лишити user-сервіс, але enrollment робити root-юнітом, який стартує за path/inotify на cgroup
   (є вікно між стартом llama і enrollment: перші відкриття NVIDIA відмовляться).
Рекомендація: варіант 1.

## Конфіг (генератор, лише друк у stdout)
    ./egpu-service-roles-config.py --hardware-config /etc/egpu-nvidia/hardware.conf \
      --role compute:/var/home/keefeere/.local/bin/llama:/sys/fs/cgroup/<cgroup-llama>:1000

## Невідоме, перевіряється лише trial-ом
SELinux: чи дозволяє `init_t` зберігати FD файлу `gconf_home_t` у FD store (для NVIDIA char-вузлів
ioctl був заборонений і ми перейшли на re-open). Якщо ні — той самий підхід: не зберігати exe в
systemd, а перевідкривати за шляхом з конфігу і звіряти inode/dev (потребує зміни adopt).
