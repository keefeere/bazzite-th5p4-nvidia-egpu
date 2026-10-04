# Відкладене завдання для Codex: профілі Gaming / Work у наявному eGPU-helper

Дата: 2026-10-03.
Репозиторій: `keefeere/bazzite-th5p4-nvidia-egpu`.
Статус: реалізація й контрольовані live-тести тривають; три профілі ще НЕ готові.

## START HERE — передача наступному агенту, 2026-10-04

Користувач попросив зупинити нову розробку через ліміт і залишити **зроблено / TODO**.
Цей розділ — найсвіжіший; старі розділи нижче є історією, не поточним дозволом.
Мета НЕ завершена. Не встановлювати candidate на ПК і не оголошувати профілі готовими.

### Оновлення 2026-10-04 (пізніше): TODO 2 і частина 3 зроблені в VM

- Fork `cardwire-daemon`: feature `service-roles` (default OFF), `src/service_owner.rs`,
  синхронний `main` (activation FD беруться до tokio runtime), конфіг
  `/etc/cardwire/service-roles.toml` (`enabled=true`, bpf_object, devices, `[[role]]`),
  drop-in приклад `assets/cardwired-service-roles.conf.example`. Існуючі pins без FD
  відхиляються, помилка = лог + legacy політика.
- Root-only D-Bus `org.opengamingcollective.cardwire.ServiceRoles`
  (`SetPermissions(au)->t`, властивості Generation/Permissions) у `interface/service_roles.rs`;
  атомарна заміна масок, каталог фіксований. Ре-enrollment/registration НЕ реалізовано.
- Тести: daemon 94 (OFF) / 98 (ON), userspace 36 (ON), clippy -D warnings і fmt чисті.
- VM (rootless, 2 virtio, без host GPU), справжній cardwired.service замінено native бінарником
  SHA256 `e0389cb7423ee058b9a7a983b2df220ee1801fc4ee177fd1141d2fe7375ddd7e`:
  `INTEGRATED_DAEMON_VM_PASSED` (create deny-all; non-root/invalid mask rejected; root commit
  допускає лише роль; SIGKILL -> adopt gen 2; stop/start; порожній FD store відхилено;
  підмінена map не adopted) і `INTEGRATED_DAEMON_DISABLED_SUITE_PASSED` (без конфігу повний 2-GPU suite).
  Логи `/tmp/cardwire-parent-vm.Rm4N5B/integrated-probe-3.log`, `integrated-suite-2.log`.
  Driver `nix/integrated-daemon-vm-test.py` (+`integrated-daemon-probe.py`), бінарник збирався в
  `cardwire-pr-validation:/build/target-sr` (НЕ `/build/target`). Нічого не commit/install на host.
- Лишається: registration/re-enrollment, реальна NVIDIA inventory + політики профілів,
  helper/widget/CLI, host-тест (потрібна згода), docs/CI/commit.

### Оновлення 2026-10-04 (ще пізніше): профілі + генератор конфігу

- Daemon: `[[profile]] name/permissions` у service-roles.toml (валідація імен, довжини, бітів),
  D-Bus `ApplyProfile(s)->t`, властивості `Profiles`, `CurrentProfile` (виводиться з живого
  kernel snapshot, не зберігається окремо). Daemon тести 100 (ON), clippy/fmt чисті.
- VM `INTEGRATED_DAEMON_VM_PASSED` (integrated-probe-4.log): + named profiles, non-root/unknown
  відхилено, adoption з правильним generation 4.
- Helper: `egpu-service-roles-config.py` — read-only генератор TOML з реальної інвентаризації
  NVIDIA вузлів (за PCI identity з hardware.conf) і ролей `KIND:EXE:CGROUP:UID`; відображення
  gaming-nvidia / work-nvidia / work-igpu → маски. Тест `tests/test-service-roles-config.py` (4 OK),
  усі helper tests проходять. Dry-run на host (stdout) дав 7 вузлів.
- Відображення profile→device — СТАРТОВА гіпотеза (напр. render node для display-ролі у
  work-nvidia); перевіряється лише host-тестом із згодою. Нічого не встановлено/не закомічено.
- Лишається: registration/re-enrollment нових процесів/cgroup (каталог фіксований; restart llama
  = нова cgroup), інтеграція в helper widget/CLI (apply/rollback/logout), host-тест, commit/install.

### Оновлення 2026-10-04 (фінал сесії): re-enrollment, ctl, commits

- Fork `persistent.rs`: manifest MAGIC CWPST002 з `epoch`; імена FD `cw-exe-{slot}-{incarnation}`,
  `cw-cg-…`, `cw-manifest-{epoch}`; `PersistentGuard::reenroll` (нові FD -> barrier -> один swap
  kernel snapshot = commit record -> прибирання старих); `adopt` вибирає єдиний manifest, що
  відповідає active snapshot, і прибирає лише правдоподібні залишки перерваної транзакції.
  `Notifier::remove` (FDSTOREREMOVE). Роль з спільними (dedup) exe/cgroup re-enroll-ити не можна.
- Daemon: root-only `ServiceRoles.ReEnrollRole(uss)->t`. VM `integrated-probe-5.log`:
  INTEGRATED_DAEMON_VM_PASSED з re-enrollment + SIGKILL після нього (adopted generation 5), старий
  persistent-snapshots регресійно PASS (`persistent-regression-3.log`), disabled-suite PASS раніше.
  Crash-вікна транзакції покриті ЛИШЕ unit-тестами (select_manifest/stale_names), не VM-ін'єкцією.
- Helper: `egpu-service-roles-ctl.py` (status / apply з readback і rollback на попередній профіль /
  enroll INDEX UNIT для ExecStartPre=+), тест `tests/test-service-roles-ctl.py` (7 OK).
- Commits (локально, БЕЗ push): fork `55bbc8f` (branch backport/v0.12.3-secure-policy),
  helper `a89e8bd` (+ ctl окремим commit). Користувач погодив commit; install/host-тест НЕ виконані.
- ЧОМУ host-тест зараз небезпечний: guard блокує ВСІ відкриття захищених NVIDIA вузлів без
  зареєстрованої ролі. Без ролей для KWin (display), CUDA/llama і звичайних gaming-програм
  увімкнення на host відрубає NVIDIA від поточної сесії. Потрібна політика для нерольових
  процесів (gaming: дозволити за замовчуванням; work: deny) ДО host-тесту.
- Наступне: (1) ввести у daemon/BPF режим «default для нерольових» per profile; (2) VM-тест
  цього; (3) bounded host trial з rollback-таймером за новою згодою.

### Оновлення 2026-10-04 (default mask для нерольових процесів) — ЗРОБЛЕНО в VM

- Snapshot ABI: поле `reserved` -> `default_mask` (розмір 664 незмінний): пристрої, які може
  відкрити БУДЬ-ЯКИЙ процес. 0 (початково) = лише ролі. BPF `service_open` повертає 0, якщо
  `default_mask & access`. Профіль: `default_mask` у `[[profile]]`, `ApplyProfile` застосовує
  маски ролей і default одним generation; `CurrentProfile` порівнює обидва; property `DefaultMask`.
- Генератор: gaming-nvidia default = усі вузли; work-nvidia/work-igpu = 0. ctl status показує default_mask.
- VM `integrated-probe-6.log`: INTEGRATED_DAEMON_VM_PASSED (+ профіль everyone: non-role відкриває,
  потім roles-only знову відхиляє); регресії persistent-snapshots, service-snapshots, disabled-suite PASS.
  BPF об'єкт перекомпільовано clang 14 з `crates/cardwire-ebpf/src/service_guard.bpf.c`.
- Тепер host-trial вже не відрубає NVIDIA: профіль gaming-nvidia (default all) = звичайний доступ.
  ЛИШАЄТЬСЯ: сам bounded host trial (потрібна згода на конкретний план: install daemon із
  feature, drop-in, конфіг, таймер відкату) і перевірка гіпотези мапінгу work-профілів на залізі.

### Оновлення 2026-10-04 (host-trial підготовлено, НЕ запущено)

- VM `integrated-probe-7.log`: + GC залишків перерваного re-enrollment (стороннє збережене ім'я
  `cw-cg-0-8` прибирається при adoption) і відмова adoption на чужому дескрипторі (`cw-bogus`);
  enforcement лишається. Драйвер тепер NotifyAccess=all (лише VM).
- Staged артефакти (git-ignored): `cardwire-stable-process-access/dist/local-service-roles-a30e8d43e4a4404a/`
  (bin/cardwired SHA a30e8d43…, service_guard.bpf.o SHA e6b22a22…, SHA256SUMS).
- `diagnostics/test-cardwire-service-roles.py` (+ `tests/test-cardwire-service-roles.py`, 7 OK):
  transient unit з RuntimeMaxSec=150s і ExecStopPost-відкатом (stop, clean fdstore, видалення pins,
  drop-in, конфігу, повернення secure 6213983). `--start` = лише Gaming (default all) + nvidia-smi +
  відкриття вузлів користувачем + незмінність KWin/llama. `--start-with-deny-probe` додатково на мить
  застосовує work-nvidia і перевіряє відмову НОВОМУ нерольовому процесу, одразу повертає gaming.
  На host НЕ запускалось; потребує `sudo` користувача.
- Короткий deny-all інтервал при старті guard (до ApplyProfile, ~1 с): нові відкриття NVIDIA тоді
  відмовляються; вже відкриті FD не зачіпаються.

### Оновлення 2026-10-04 (перший HOST-trial: FAILED чисто, причину знайдено й виправлено)

- Користувач сам запустив `test-cardwire-service-roles.py --start` (12:42). Результат: trial FAILED
  (`Unknown interface ...ServiceRoles`), відкат `RESTORED` ідеально: cardwired 1713304 secure,
  Hybrid `u 1`, FD store 0, drop-in/конфіг/pins прибрані.
- Причина (на VM не відтворюється): `service-roles disabled ... unexpected owner FD store count`.
  SELinux на host: `AVC denied { ioctl } ... scontext=init_t tcontext=xserver_misc_device_t`
  на /dev/nvidia0, nvidiactl, nvidia-uvm, nvidia-uvm-tools, nvidia-modeset: PID1 не може зберігати
  NVIDIA char-device FD. Guard тому не стартував; демон працював у legacy.
- Фікс (форк): device FD БІЛЬШЕ НЕ зберігаються в systemd. `PersistentGuard::adopt(held, devices, ...)`
  приймає перевідкриті за конфігом вузли і звіряє їхні rdev із inventory у manifest
  (ідентичність, не retention). `names()` без `cw-dev-*`. Старі store-и з cw-dev-* відхиляються.
  Exe/cgroup FD лишаються в systemd (їх SELinux не блокував; для реальних ролей з home-шляхами
  це ще треба перевірити trial-ом).
- VM: INTEGRATED_DAEMON_VM_PASSED 3/3 стабільно (+ persistent-snapshots PASS, disabled-suite PASS).
  Флейк тест-хелпера виправлено (sender має лишатись у cgroup юніта до обробки FDSTORE).
- Нова збірка: dist/local-service-roles-99013ecd90a08bb4 (cardwired SHA 99013ecd…,
  object e6b22a22…). Обгортка оновлена + дія `--archive-restored` (потрібна, бо старий
  /run/egpu-cardwire-service-roles-test лишився; `--start` його не перезаписує).
- ПОРЯДОК для користувача: спершу `sudo python3 .../test-cardwire-service-roles.py --archive-restored`,
  потім `--start`.

### Оновлення 2026-10-04 13:25 (ДРУГИЙ HOST-trial PASSED: Gaming profile)

- Користувач запустив `--archive-restored`, потім `--start`. Журнал: `service-roles: created generation 1`,
  `GAMING PROFILE OK: guard active, user opens NVIDIA nodes, session unchanged.`, `RESTORED`.
  Після: cardwired секюрний, Hybrid `u 1`, FD store 0, drop-in/pins прибрані, 0 AVC denials.
  Це перше підтвердження guard + persistent owner + SELinux-фікса на РЕАЛЬНОМУ ядрі 7.2 / NVIDIA.
- Побічна знахідка: у вікні deny-all власне Vulkan-зондування cardwired отримало
  `VK_ERROR_INCOMPATIBLE_DRIVER` на renderD129. Фікс: `initial_profile` у service-roles.toml
  (застосовується один раз при СТВОРЕННІ guard, не при adoption). VM 2/2 + suite PASS; генератор і
  обгортка тепер ставлять initial_profile = gaming-nvidia.
- Нова збірка: dist/local-service-roles-72f973bd8b06de59 (cardwired SHA 72f973bd…); SHA в обгортці оновлено.
- НЕ перевірено на host: work-nvidia / work-igpu enforcement (deny-probe), real KWin/llama ролі,
  ExecStartPre enroll. Наступний крок користувача: `--archive-restored`, потім `--start-with-deny-probe`.

### Оновлення 2026-10-04 13:32 (ТРЕТІЙ HOST-trial PASSED: Gaming + Work-NVIDIA deny)

- `--start-with-deny-probe` (13:31:54): `service-roles: created generation 2` (initial_profile спрацював,
  Vulkan-шум VK_ERROR зник), `GAMING PROFILE OK`, `WORK PROFILE DENY OK (errno 13); reverted to gaming`,
  `RESTORED`. 0 AVC. Після: cardwired secure 1815356, Hybrid, FD store 0, drop-in прибрано.
- Підтверджено на реальному ядрі 7.2/NVIDIA: профіль work-nvidia відмовляє НОВОМУ нерольовому
  процесу відкрити /dev/nvidia0, gaming-nvidia дозволяє. Існуючі FD/KWin/llama не зачеплені.
- Лишається (потребує окремих рішень): справжні ролі KWin(display)/llama(compute) і їх SELinux
  поведінка для exe/cgroup FD; ExecStartPre enroll у unit llama; тривале використання (не 150 с);
  work-igpu enforcement; install у /usr (зараз лише transient trial); commit-и локальні, push не робився.

### Примітка 2026-10-04: llama-роль — обмеження дизайну (див. examples/llama-service-role.md)

llama — USER-сервіс, а ReEnrollRole root-only: user-unit не може сам зробити enroll. Рекомендація:
системний сервіс `User=keefeere` + `ExecStartPre=+ ... egpu-service-roles-ctl.py enroll`. SELinux для exe
`gconf_home_t` невідомий (можлива потреба re-open замість FD store, як для NVIDIA вузлів).

### Оновлення 2026-10-04 13:45: llama-role trial ПІДГОТОВЛЕНО (не запущено)

- Користувач обрав ролі llama і KWin та дозволив llama як системний сервіс. `--start-llama-role`
  у diagnostics/test-cardwire-service-roles.py: знімок живого llama (argv, cwd, whitelisted env), guard з роллю
  compute=llama (поточна cgroup), далі gaming OK -> stop user llama -> work-nvidia -> transient SYSTEM unit
  `egpu-llama-role-test` (User=keefeere) з `ExecStartPre=+ ctl enroll 0 ... --exe <llama>` -> чекає порт 9931
  (до 120 с) -> перевіряє, що llama ТРИМАЄ /dev/nvidia* під work-nvidia і що звичайний процес отримав errno 13
  -> gaming -> stop transient -> start user llama. Відкат (ExecStopPost) завжди повертає user llama.
  RuntimeMaxSec=330s. Тести 11 OK. Побічний ефект: llama недоступний ~1-3 хв; GPU-програми в цей час не запускати.
- KWin-роль: trial НЕ готовий свідомо — потребує перезапуску сесії (exec KWin під guard); потрібна окрема згода.

### Оновлення 2026-10-04 13:40: llama-role trial #1 — enrollment ПРАЦЮЄ, exec заблокував SELinux

- Журнал 13:39: gaming OK; `ReEnrollRole` через ExecStartPre прийнято (generation 5, cgroup
  system.slice/egpu-llama-role-test.service, exe FD gconf_home_t збережено в FD store — SELinux це
  дозволив). Далі `status=203/EXEC`: `AVC denied { execute } scontext=init_t tcontext=gconf_home_t`
  — системний unit не може exec-нути бінарник з home. Відкат чистий, user llama відновлено (PID 1830947).
- Фікс: ExecStart = `/usr/bin/setpriv --reuid=keefeere --regid=keefeere --init-groups -- <llama> args`
  (bin_t -> unconfined_service_t, далі exec llama з uid 1000). Додано reset-failed transient. Тести 11 OK.
- Наступне: `--archive-restored`, потім `--start-llama-role` знову.

### Мета і межі

Три профілі в НАЯВНОМУ eGPU-helper + Cardwire:
1. Gaming: NVIDIA render і NVIDIA displays, чинні gaming можливості збережені.
2. Work/NVIDIA displays: AMD render, NVIDIA scanout з вузькими display exceptions.
3. Work/iGPU displays: AMD render/scanout, NVIDIA лише явно дозволеним compute.

Ізоляція звичайних системних демонів від NVIDIA, з погодженими вузькими
driver/display/inference винятками. Не blanket-allow root, всю Plasma, launcher
або всю user session. Не ламати llama/workers при зміні профілю. Display topology
не константа: визначати фактичний DRM шлях; DisplayLink != автоматично iGPU.
Не створювати другий production GPU manager. Fork Cardwire погоджений пізніше
за старий початковий план унизу цього файла; зміни робляться саме в ньому.

Дозволено зараз: source/build/offline tests/rootless disposable VM. НЕ дозволено
в межах поточного продовження: host install/restart Cardwire, GPU mode switch,
logout/SDDM/KWin restart, llama restart, reboot/suspend, PCI/kernel/USB4 changes,
commit/push. Для цього потрібне нове окреме підтвердження. Power action не міняти.
User не хоче kernel patches. Незалежні dirty sleep/PM зміни зберегти.

### Зроблено й доведено

- Host live exact-Smart trial раніше завершився: користувач підтвердив видимий
  куб без артефактів. Відновлено secure fork/Hybrid. Це не перевірка повних профілів.
- ABI/BTF/exact policy та single-role synchronous exec/open кандидат із попередніх
  turns описані нижче. Важливо: VM довела fork не успадковує ticket, threads
  дозволені, exec повторно перевіряється, daemon restart може втратити старі
  legacy grants — тому не використовувати async PID allow replay як фінальний дизайн.
- Попередній turn додав immutable multi-role snapshots; поточний **об'єднав їх
  із persistent ownership для ФІКСОВАНОГО каталогу registrations/devices**.
- Новий reusable backend:
  `cardwire-stable-process-access/crates/cardwire-ebpf-userspace/src/service_guard/persistent.rs`.
  За `experimental-service-roles` (default OFF). Нема production caller.
  `PersistentGuard::create/adopt/prepare_permissions/commit`:
  identity FDs зберігає PID1; maps/exec+open links pinned у private root bpffs;
  sealed memfd manifest зв'язує role/device identity, ABI, map/link/program IDs.
  Names deduplicated по held identity, не pathname. Перевіряється exact FD set,
  owner MainPID/store capacity/preserve/count, frozen inventory та active snapshot.
- Profile permission update = fresh frozen ARRAY + один ARRAY_OF_MAPS swap.
  Active inner map є commit record; немає другого marker, який може відстати
  після crash. Prepared transaction прив'язана до outer-map ID і base generation;
  stale/foreign transaction rejected. Каталог незмінний, тому permission updates
  не накопичують нові retained role refs і не мають старого ліміту256 generations.
- `fdstore.rs` додано startup-only `take_namespaced_activation("cw-")` для
  variable manifest set; strict complete-set validation виконує adopter.
  Non-UTF8 LISTEN_* тепер error, не мовчазне трактування як absent. Старий
  exact-name `take_activation` збережений. Це не викликається default daemon.
- VM frontend `examples/persistent-snapshots-vm.rs` і
  `nix/persistent-snapshots-probe.py`. Це одноразовий test service, не другий
  production manager. Driver `nix/service-role-vm-test.py` тепер має4 probes.
- PASS у disposable Linux6.18.46/two virtio/no host passthrough:
  root-owner SIGKILL **before** commit -> old policy; **after** commit -> new policy;
  доступ перевірений ПІД ЧАС відсутності owner; new allowed workers допускаються;
  outsider denied; inference-stand-in PID незмінний; stale/invalid requests rejected;
  300 permission swaps, FD store6 і local FD count bounded; normal stop/start;
  чужа same-ABI pinned inventory map rejected без заміни attached enforcement;
  recreated cgroup не авторизується самочинно. Це НЕ real CUDA/llama test.
- Початковий run exec3016 exit0,16.33s:
  `/tmp/cardwire-parent-vm.Rm4N5B/persistent-snapshots-vm-1.log`.
  Після додаткового binding prepared transaction до owner ID зібрано знову;
  exact-source regression уже дає `PERSISTENT_SNAPSHOTS_VM_PASSED` у
  `persistent-snapshots-regression-20261004.log` і старий multi-role
  `SERVICE_SNAPSHOTS_VM_PASSED` у `service-snapshots-regression-20261004.log`.
- Unit/clippy exec20986 exit0:
  `persistent-snapshots-unit-clippy.log`: default userlib34 tests, opt-in36
  (2new manifest/catalog tests), example3 shared ABI fixture tests;
  release builds трьох examples; clippy --no-deps -D warnings PASS.
  Python syntax і git diff --check PASS.
- Final candidate artifact `/tmp/cardwire-parent-vm.Rm4N5B/cardwire-persistent-snapshots`
  SHA256 `e8a11967ca65c156e0df44804a547db324fa0baa50cf9995a703699a13f40eaf`.
  Persistent source SHA256
  `62e0c67e0721f383f460cedc96280fa6b6e76fd160b61f1a5dd4c3cdee7cb514`;
  fdstore source `4e909ce0c769420cfe960c6f1aa15459d503367858ff7b9885e488309b5d2a0e`.

### Що ще НЕ зроблено / TODO у пріоритеті

1. Прочитати актуальні source/diff та цей handoff. Остання regression batch
   завершена: всі3 probes PASS, exec11520 exit0; повторювати після змін, не
   продовжувати вже закритий handle. Детальні докази в логах нижче.
2. Інтегрувати opt-in ownership у **існуючий cardwired**, не новий daemon.
   Entry: `crates/cardwire-daemon/src/daemon.rs`, зараз `#[tokio::main]`.
   Activation FDs треба приймати ДО створення runtime/threads. Startup catalog,
   ordering, bootstrap-failure recovery та explicit deactivation ще не готові.
   Unit `assets/cardwired.service` зараз Type=dbus/ProtectSystem=strict/NO FDstore;
   нічого з цього на host не змінено. Врахувати потрібні narrow unit permissions.
3. Authenticated service registration/start/adoption/re-enrollment protocol.
   Поточний persistent catalog ФІКСОВАНИЙ: replacement exe/cgroup/device не додається
   автоматично. Restart llama/systemd-unit створює нову cgroup; це ще gate, не
   вирішено самим permission commit. Root-auth API, stable held refs, first-open
   admission, вузьке pidfd adoption для вже запущених processes, failure recovery.
   KWin unit MainPID — wrapper, не композитор; не allow весь його cgroup.
4. Реальна NVIDIA inventory (DRM + NVIDIA control/UVM/CUDA nodes), lifecycle після
   hotplug, profile-specific policy для звичайних apps і строгих service exceptions.
   Candidate наразі блокує всі protected opens без registered role, це ще НЕ
   придатна gaming app policy. Legacy Hybrid/env/comm bypass не має відкрити
   системним демонам обхід strict guard. Не міняти старий режим без explicit opt-in.
5. Зв'язати 3 profiles у helper/widget/CLI: planned vs applied, topology checks,
   narrow permissions, погоджений logout, timeout/rollback, locks із detach/boot.
   Існуючий `egpu-desktop-profile-plan.py` — planner, не готовий switcher.
6. VM acceptance саме integrated cardwired: startup, kill/restart, API auth,
   registration churn, no broad grants, rollback; старі kernel paths не ламати.
7. Лише з новою згодою користувача: bounded host trial, реально desktop rendering,
   NVIDIA displays та CUDA/llama/workers, native/Flatpak/Electron, measured VRAM,
   усі3 profiles, service restart, revert. Не оголошувати результат за virtio alone.
8. Після повного review/tests — docs, CI/packaging/reproducible build, окремо
   погоджені commit/push/install. Усі нинішні candidate source edits НЕ committed.

Межі безпеки: existing/inherited/passed GPU FDs не відкликаються. Trusted root
може змінити BPF; це не hostile-user security sandbox. Початкове create ще має
неперевірені crash windows до READY; partial refs/pins зберігаються навмисно й
adoption відмовляє, не автоматично repair. Prepared/commit proof стосується
ВЖЕ повністю створеного owner. Не приховувати ці обмеження.

### Поточний стан ПК, repos і відтворення

- Read-only перевірка під час handoff: cardwired active PID1294625, invocation
  `a6c1434fdeb64fb4bce9643a2a0d2c97`, Mode `u 1` (Hybrid).
  User KWin MainPID187650, invocation `ff4db9d153cd4c159296be85edcaa666`.
  User llama.service PID5151, invocation `efe4e0b1876c4393ad62c023d1ebbe12`,
  worker17712. Host не рестартувався й ці служби не чіпалися.
- Runtime AMD-first KWin override лишається після погодженого попереднього trial:
  `/run/user/1000/systemd/user/plasma-kwin_wayland.service.d/90-egpu-amd-primary-test.conf`.
  Це не завершений persistent Work profile. Installed Cardwire secure6213983,
  override `/etc/systemd/system/cardwired.service.d/95-local-secure-policy.conf`.
- Fork `/var/home/keefeere/_repos/_home/cardwire-stable-process-access`, branch
  `backport/v0.12.3-secure-policy`, HEAD6213983, багато попередніх dirty changes.
  Helper `/var/home/keefeere/_repos/_home/bazzite-th5p4-nvidia-egpu`, HEADef95d64,
  main ahead1 та незалежні dirty sleep/PM files. Не reset/checkout/stash навмання.
- Compiler container `cardwire-pr-validation`, source `/build/exact-policy.ZGkn6H`,
  target `/build/target`; Rust1.95.0, pinned BPF nightly2026-08-12, clang14.
  Host НЕ має отримувати toolchain/packages. C object
  `/build/role-probe/service_guard.bpf.o`; source `crates/cardwire-ebpf/src/service_guard.bpf.c`.
- **НЕ DEPLOY `/build/target/release/cardwired`**: там давніший cached binary з
  temporary LSM edit, source після цього змінений. Current turns збирали examples
  і libraries, не production daemon. Verified dist/local exact candidate не чіпали.
- Rootless VM build container `cardwire-policy-vm-build-20261004`;
  артефакти `/work/parent-diagnostic`; без host PCI/GPU passthrough.
  Driver `/nix/store/axfk201w5j7xihkqd2inlibh303a59mk-nixos-test-driver-cardwire-test/bin/nixos-test-driver`;
  config `/nix/store/fb275v240qgzl6s935q5ixp0ss1pagiy-driverConfiguration.json`.
  `--test-script /work/parent-diagnostic/service-role-vm-test.py`;
  `CARDWIRE_ROLE_ARTIFACT_DIR=/work/parent-diagnostic`;
  select `CARDWIRE_ROLE_PROBE=persistent-snapshots-probe.py`.
  Python `/nix/store/b5bpi6zfajzzrwwpgba2q6li3nnya4bs-python3-3.14.7/bin/python3`;
  ELF loader `/nix/store/n51dhmdbik1kfrsm62j5knavmigwrl1a-glibc-2.42-84/lib/ld-linux-x86-64.so.2`;
  libs same glibc lib + `/nix/store/0vqb1mcas5j8dv6bhbrshinlgsg6bvgi-gcc-15.3.0-lib/lib`.
  Env prefixes: `CARDWIRE_ROLE_PYTHON`, `CARDWIRE_ROLE_ELF_LOADER`,
  `CARDWIRE_ROLE_LIBRARY_PATH`. Output directory must already exist.
- **Фінальна перевірка перед передачею:** batch exec11520 завершився exit0.
  Усі3 логи `persistent-snapshots-regression-20261004.log`,
  `service-snapshots-regression-20261004.log`,
  `service-role-owner-regression-20261004.log` у
  `/tmp/cardwire-parent-vm.Rm4N5B/` містять відповідні *_VM_PASSED.
  Старий exact-name FDstore owner regression теж пройшов після transport changes.
  Наших незавершених build/VM test handles немає. Containers не видалялися.

Не продовжувати широкі hardware тести через цей handoff. Спочатку прочитати
актуальний worktree, потім наступний TODO; подальший host тест лише погоджений.

## Продовження 2026-10-04: immutable multi-role snapshot backend VM PASSED

Попередній turn — PROGRESS: single-role owner crash/FDstore adoption. Цей turn
додав окремий opt-in backend для цілісного оновлення кількох ролей. Host GPU
authority не розширено; host mode/install/session/llama НЕ змінювалися.

- Shared ABI `crates/cardwire-policy/src/service_roles.rs`: версія1,
  Snapshot664/Role40/Inventory72/Ticket16, до16 roles/nodes. Header/mask/unused
  fields/duplicates/generation перевіряються до publication. Policy generation
  відділена від admission incarnation: незмінному inference worker не треба
  exec/restart при зміні дозволів графічної ролі. Removed/replaced/reordered role
  не може повторно використати старий ticket.
- `crates/cardwire-ebpf-userspace/src/service_guard.rs` за feature
  `experimental-service-roles` (default OFF, production caller відсутній).
  API attach explicit char inventory, Registration із held exe/cgroup refs,
  publish fresh frozen ARRAY -> один ARRAY_OF_MAPS pointer replacement.
  Validation/allocation/freeze перед commit; retired refs conservatively held,
  limit256 generations fail-before-mutation, без unsafe eviction.
- `crates/cardwire-ebpf/src/service_guard.bpf.c`: CO-RE task-storage exec/open
  hooks із одним immutable snapshot pointer на decision, exact UID/EUID/exe/
  cgroup + per-node access mask. New object не включений в default build.rs.
  Clang14 compile і guest verifier/load успішні. Без патчів ядра.
- Guarded `examples/service-snapshots-vm.rs` використовує реальний backend,
  а не незалежну копію логіки. Новий `nix/service-snapshots-probe.py` і третій
  allowed variant у `nix/service-role-vm-test.py`.
- PASS real opens: initial deny-all; дві ролі/два захищені nodes; threads allow,
  fork/outsider deny; permission-only revoke/restore без exec; inference PID
  незмінний;24 додаткові swaps; duplicate/invalid/stale rejection leaves old
  state; kernel freeze EPERM; clear/reintroduce не revive старі tickets;
  recreated cgroup вимагає registration+exec, друга роль продовжує працювати;
  rdev aliases покриті; teardown звільняє тільки цей guard.
- VM exec20218 exit0,12.79s. Rootless/two virtio/Linux6.18.46/no host GPU.
  Evidence `/tmp/cardwire-parent-vm.Rm4N5B/service-snapshots-vm-1.log`;
  container `cardwire-policy-vm-build-20261004:/work/service-snapshots-result-1`.
  Native tested artifact SHA256
  `c10503e6bf493dfcbfcc199ae205e970204542e81c7ff4f218726447bfb36bca`;
  object `42f50ed43836fea52c49ecee3c978f7fb2f351e260b61fa0bf1af25e9028da6f`.
  Rustfmt-only source pass після VM, без semantic changes.
- Unit/clippy exec12483 exit0: policy16 tests (5new), userspace34 tests with
  feature OFF і ON, example3 shared fixture ABI tests; clippy --no-deps
  -D warnings PASS. Log `service-snapshots-unit-clippy.log`. Default daemon
  behavior/legacy tests залишилися незмінні. Нема deploy/commit/push.
- Host read-only recheck: cardwired PID1294625 invocation
  a6c1434fdeb64fb4bce9643a2a0d2c97, Hybridu1; KWin MainPID187650 invocation
  ff4db9d153cd4c159296be85edcaa666; user llama.service PID5151 invocation
  efe4e0b1876c4393ad62c023d1ebbe12, worker17712. Все як до роботи.

ВАЖЛИВО: цей multi-role owner UNPINNED і не використовує fdstore. Його exit
прибирає enforcement. Попередній crash/adoption proof — ІНШИЙ single-role
fixture; НЕ стверджувати crash-safe multi-role profiles на підставі двох окремих
тестів. Новий VM worker лише імітує inference доступ, не CUDA/llama proof.
Existing/passed FDs не відкликаються, hostile-user sandbox не заявляється.

Наступний змістовний крок: об'єднати snapshot publication та persistent owner
protocol усередині існуючого Cardwire, із crash-safe publication manifest і
bounded retained-reference lifecycle; потім authenticated registration/start/
re-enrollment API, NVIDIA inventory та три profile policies. Не створювати
другий production manager. Host install/logout/llama restart лишаються окремо
погоджуваними. Всі три профілі ще НЕ готові, goal ACTIVE, не blocked.

## Продовження 2026-10-04: actual owner crash + FD-store adoption VM PASSED

Попередній goal turn — PROGRESS: synchronous single-role guard + stopped guest
Cardwire proof. Цей turn просунув саме ownership: тепер без controller-held
exe/cgroup FDs і з SIGKILL справжнього керуючого test owner, не стороннього daemon.
Host permission scope не розширено, нових live-переходів не було.

- Fork: новий reusable `crates/cardwire-ebpf-userspace/src/fdstore.rs` (explicit
  startup-only activation FD ownership, strict PID/names/count, CLOEXEC,
  SCM_RIGHTS, FDPOLL=0, bounded barrier). Module compiled, але production
  cardwired його НЕ викликає. Cargo додано direct `libc="0.2"`, lock використовує
  вже pinned0.2.189, жодного package update/install.
- Новий `examples/service-role-owner-vm.rs` intended для наступного переносу
  lifecycle всередину наявного cardwired, НЕ другий production GPU manager.
  Фіксований unit `cardwire-role-owner-vm.service` тільки в guarded virtio VM;
  Type=notify/NotifyAccess=main, StoreMax3/Preserveyes, Restart=no.
- BEFORE attach: executable і cgroup refs збережені PID1, barrier + readback
  NFileDescriptorStore2. AFTER attach: sealed memfd manifest з config/IDs,
  map ABI, link/program identities збережений як третій FD, count3 перед READY.
  При restart точний набір names, sealed manifest, config/held object identity,
  map ABI/IDs і program map references мусять збігатися. Partial/missing state
  не rebuild-иться самочинно. FD store не чиститься при exit/drop.
- `nix/service-role-owner-probe.py` не тримає exe/cgroup FDs. PASS: service
  SIGKILL -> actual MainPID0/failed -> дозволений live/new worker працює, fork
  та outsider denied; новий owner adopts без replacement hooks; stop/start;
  same-path replacement executable denied; same-ABI чужа TASK_STORAGE map
  rejected; original guard продовжує enforcement; restore original map
  дозволяє adoption; recreated cgroup path denied без silent re-registration.
- VM exec39098 exit0,14.84s. Evidence:
  `/tmp/cardwire-parent-vm.Rm4N5B/service-role-owner-vm-2.log`;
  container `/work/service-role-owner-result-2`. Driver source
  `nix/service-role-vm-test.py`, optional `CARDWIRE_ROLE_PROBE` selects only the
  existing two accepted probes. Guest6.18.46/rootless/two virtio, без host GPU.
- Перший run `service-role-owner-vm-1.log` теж довів crash/adoption і rejected
  bad map, але recoverystart6 потрапив у default StartLimitBurst; cleanup failed
  на failed unit. Fixture тепер explicit reset-failed ONLY own unit перед
  recovery, перевіряє store still3; cleanup stop/reset-failed/clean ownfdstore.
  Це harness issue, не виправлення host systemd policy або GPU regressions.
- Unit/clippy exec72840 exit0: library34 tests PASS, кожен example3 ABI tests
  PASS (ті самі3 fixture тести, не6 незалежних); clippy --no-deps -D warnings
  PASS. `service-role-owner-unit-clippy.log`. Python syntax і diff check PASS.
- Native owner artifact `/tmp/cardwire-parent-vm.Rm4N5B/cardwire-role-owner`,
  SHA256 `614cea28c521adbac5054d0c3ba4dff30f626f0bf63e8575bb6fe9c2b694ab86`.
  Був зібраний до rustfmt-only pass; семантичних змін після його VM тесту немає.
  Попередній stale `/build/target/release/cardwired` НЕ використовувався і
  залишається непридатним для deploy без нової точної збірки.

Межі: transport+single-role example, не production fdstore integration. Немає
authenticated unit registration/start gate, atomic multi-role generations,
всіх NVIDIA nodes, профільних gaming/work policy, UI/rollback та реального
service/cgroup restart admission. Усі три цільові профілі лишаються незавершені.
Наступний змістовний крок: versioned multi-role policy/atomic publication +
systemd start/re-enrollment protocol, перенос owner lifecycle в Cardwire за
explicit opt-in; не додавати unbounded async allow replay або широкі user grants.
Host GPU changes/install/logout/llama restart досі потребують окремої згоди.

## Продовження 2026-10-04: synchronous service-role VM candidate PASSED

Це продовження після підтвердження користувачем куба без артефактів; нового
host trial НЕ було. У fork додано окремий VM-only кандидат, не production hook:
`nix/service-role-guard.bpf.c`, `nix/service-role-worker.c`,
`nix/service-role-probe.py`, `nix/service-role-vm-test.py`,
`crates/cardwire-ebpf-userspace/examples/service-role-vm.rs`.
Reproduction/build описано у `docs/development/smart.md`.

- C CO-RE зберігає typed task pointers для TASK_STORAGE helpers. Aya0.14
  завантажує object з explicit `allow_unsupported_maps()` ТІЛЬКИ в example.
  Окремі exec/file_open hooks, три maps та обидва links pinned; loader виходить.
  Production cardwired цю програму не завантажує. Патчів ядра немає.
- Guest6.18.46/x86_64/two virtio GPUs, rootless QEMU без host GPU passthrough.
  Loader, worker та BPF object збудовано native container Rust1.95/clang14;
  existing guest Cardwire package лишився незмінним. Не host7.2 proof.
- PASS: static ELF constructor відкриває protected render node ДО `main()`;
  threads дозволені; fork-before-exec не успадковує admission; leader і
  nonleader exec revalidate. Неправильні executable/UID/cgroup та descendant
  cgroup заборонені; live cgroup membership перевіряється на кожному open.
- PASS: вже запущений process поза role не отримує доступ від простого move
  у cgroup; окремий privileged pidfd TASK_STORAGE admission дає доступ тільки
  йому й потокам, не fork child. Це primitive, не authenticated production API.
- PASS: rdev-based rule покриває інший mknod того самого пристрою; /dev/null
  не зачіпає. Зміна generation відкликає нові opens до verified exec.
  Recreated cgroup з тим самим pathname заборонений до explicit registration.
- PASS: дозволений/сторонній opens перевірено ДО зупинки, ПІД ЧАС стану
  inactive guest Cardwire й ПІСЛЯ start. Guard живе незалежно від його daemon.
  Controller досі тримає exe/cgroup FDs. `--inspect-pinned` лише читає config,
  НЕ strict ownership/ABI/link adoption. FDstore/reboot protocol ще відсутній.
- Final exec86450 exit0,12.40s: `/tmp/cardwire-parent-vm.Rm4N5B/service-role-vm-4.log`;
  container result `/work/service-role-result-4`. Попередній exec79511 exit0
  з одною restart перевіркою: `service-role-vm-3.log`. Rust ABI/dev encoding
  tests3/3 PASS (`service-role-build-4.log`), C `-Wall -Wextra -Werror`, Python
  syntax і git diff --check PASS. Default/full Nix matrix НЕ rebuild цього C
  кандидата; новий driver explicit і збережений у repo.
- Artifacts у `/tmp/cardwire-parent-vm.Rm4N5B/`:
  loader SHA256 `1d486e40de59ce44e9efc9ea567977dc51334fc1a922d978b26973111957123b`;
  BPF object `4406bbe39ab33d5c8241c91355e8e8f3d617d0d30816aa5e52e4d99a34cbca36`;
  worker `49c2ede4fb21c6128820fa39b90fe4377e5c59f168574a5da150e0254d9031f4`.

Важливе уточнення попереднього committee висновку: можливий LSM chaining bypass
НЕ відтворився. Negative expectation open==0 після restart старого Cardwire
впала, бо фактичний результат був правильний `-13`; evidence
`service-role-chain-negative.log`. x86 kernel BPF modify-return trampoline
зупиняє chain на nonzero. Не заявляти підтверджену вразливість чи її виправлення.
Тимчасову ручну prior-ret зміну трьох Rust hooks повністю знято (попередні
unrelated edits збережено); candidate C hook prior-ret шанує. **Не deploy**
`cardwire-pr-validation:/build/target/release/cardwired`: цей cached binary
зібрано під час тимчасової зміни й він НЕ відповідає відновленому source.
Перевірений dist `local-exact-btf-2e63169e3ff064cb` не змінювався.

Межі: один root role/один virtio render node, не CUDA inference workload, не
host NVIDIA inventory, не multi-role generation transaction, не sandbox від
trusted root/FD passing/already-open FDs. Потрібні daemon-owned verified
pin adoption + held object lifetime (fdstore), authenticated service start
gate, atomic complete policy generations та всі потрібні NVIDIA device nodes.
Лише потім helper profiles/rollback і окремо погоджений host synthetic trial.

Final read-only host: Cardwire1294625/invocation
`a6c1434fdeb64fb4bce9643a2a0d2c97`, Hybrid `u 1`; KWin187650/
`ff4db9d153cd4c159296be85edcaa666`; llama5151/
`efe4e0b1876c4393ad62c023d1ebbe12` — незмінні. Немає host install/restart,
mode switch, logout, llama restart, reboot, commit/push. Goal активний.

## Продовження 2026-10-04: persistent-role design і VM lifecycle baseline

Після підтвердження куба нових host-переходів НЕ було. Read-only стан:
Cardwire1294625/invocation`a6c1434fdeb64fb4bce9643a2a0d2c97`, Hybrid `u 1`;
KWin187650/invocation`ff4db9d153cd4c159296be85edcaa666`, llama5151/
`efe4e0b1876c4393ad62c023d1ebbe12` незмінні. Host install/restart/mode switch,
logout, llama restart, reboot, commit/push не авторизовані цим продовженням.

`paseo-committee`: Paseo daemon stopped; main оголосив fallback на двох
вбудованих read-only reviewers (`persistent_access_design`,
`persistent_access_safety`). Обидва завершили без edits; висновки узгоджені:
не будувати persistent exceptions на delayed analyzer/replay numeric PIDs.
Потрібні вузькі service roles, synchronous exec admission, kernel process
identity, cgroup object lifetime і pin/adopt maps **та links**. Plain cgroup+exe
матч дозволяє fork-before-exec child, тому Exact потребує окремої eligibility.
Gaming isolation має бути окремим від Hybrid bypass; strict roles не повинні
обходитися через legacy CARDWIRE_ALLOW/app/comm whitelist. Root/controller
trusted, inherited/passed GPU FDs не відкликаються цим механізмом.

У fork додано лише probes/docs та primitive subtest у `nix/ci-2gpu.nix`:

- `nix/lifecycle-baseline-probe.py` у disposable VM відтворив: async first-open
  miss; Exact grant cleared by same-PID exec; grant lost on daemon restart;
  no enforcement while daemon stopped. Це KNOWN-GAP evidence, не acceptance.
  Після майбутнього fix очікування треба замінити positive regression.
- `nix/task-storage-probe.py`: pidfd-addressed BPF TASK_STORAGE map create,
  update/read/delete, fork noninheritance, dead/reaped pidfd rejection,
  pin/reopen зі збереженням entries після закриття всіх map FDs — PASS.
  Same-process exec **зберігає** storage: exec hook мусить revalidate.
  Це raw UAPI feasibility, жодної BPF program attachment/GPU policy зміни.
  Aya0.14 maps/mod.rs представляє TASK_STORAGE як Unsupported; production
  Rust adapter, BPF helper/verifier support, threads/nonleader exec, admission,
  cgroup/executable refs і pinned enforcement поки НЕ реалізовані.
- Обидва probes обмежені root, unoptimized x86_64 Python, fixture marker,
  QEMU Standard PC і двома PCI1af4:1050/virtio-pci. Host invocation probe
  відхилено ДО BPF syscall. Не пропонувати запуск цих scripts на desktop.
- Exec69201 exit0,23.86s: existing full2GPU package suite + обидва поточні
  source probes пройшли разом. Log:
  `/tmp/cardwire-parent-vm.Rm4N5B/lifecycle-suite-vm-2.log`.
  Guest6.18.46, package `/nix/store/6861frpdqrkyfhhmmzpavwp4arg4wqnr-cardwire-0.12.3`.
  Це повтор existing production binary + нові test sources, НЕ rebuild нового
  Nix derivation/3GPU/15GPU matrix і не host7.2 primitive test. Python syntax
  та git diff --check PASS. Diagnostic runner у тому ж /tmp каталозі.
- Попередні harness failures збережені: missing guest Python PATH; DRM device
  driver symlink вказує на virtio-pci (не virtio_gpu); delete-map attr мусить
  мати value=0; після старого suite треба Hybrid до hardware guard, бо
  Integrated приховує sysfs. Не вважати їх GPU/hardware regressions.

NEXT bounded implementation: один synthetic service role у VM. Перевірити
Aya-compatible task-storage map/LSM helpers і synchronous exec admission,
fork-before-exec denial, leader threads/nonleader exec, live cgroup/exe checks.
Потім pin/adopt enforcement без open interval при daemon death; не pin лише
PID map і не replay старі PID. Припущення про LSM prior-return bypass з цього
design review пізніше НЕ підтвердилося у VM (див. новий запис вище); hot program
replacement однаково потребує власної перевіреної ownership процедури.
Cgroup array може тримати object refs, але ancestry helper ширший за exact
cgroup. Executable inode/dev потребують held object lifetime, не вічного cache.
Також вирішити stale inode/name device-node tracking. Лише після VM first-open
і restart proof — новий окремо дозволений host synthetic-service trial.
Не повторювати незмінений cube test і не оголошувати три профілі готовими.

## Продовження 2026-10-04 09:40: HOST Exact Smart/AMD-render trial PASSED, куб підтверджено

Авторитетний новий результат замінює попередній bind-mount failure як останній
тест, але НЕ означає готовність трьох постійних профілів.

- Користувач успішно виконав `--archive-restored && --start`; старий failure
  збережено в `/run/egpu-cardwire-exact-candidate-test-archive-u__j4awv/trial`.
- Новий transient trial завершився успішно за7.650s, до09:40:14 EEST. Unit
  автоматично зібрано (`could not be found` після успішного завершення — нормально).
  Journal показує `Deactivated successfully`, inner PID1293534,
  outer rollback1294387 exit success. Жодного повторного запуску агентом.
- Native daemon SHA `57b495249ae2959f15c79b41183533ee8299b88eda43f68a2ef952f2772039bd`.
  `grant_policy=Allow_dGPU_Exact`;12 чинних GPU clients grandfathered власними
  exact grants. New UID1000 і UID0 probes не відкрили NVIDIA DRM/card/render,
  nvidia0/nvidiactl, але відкрили AMD renderD128. Дозволений user CUDA probe
  відкрив GPU nodes, cuInit успішний, cuda_device_count=1.
- GLX, EGL/Wayland, Vulkan cube returncode0, позитивні AMD890M/radeonsi/RADV
  hardware renderer strings, не software rendering. Live KWin лишився AMD;
  DP-7 та HDMI-A-1 NVIDIA outputs лишились enabled і topology незмінна післявідкату.
- Користувач ЯВНО підтвердив для саме цього запуску: «куб був артефактів не було».
  Це visual confirmation короткого cube trial. Raw автоматичний report має
  `visible_output_verified=false` за задумом; цей окремий user evidence доповнює
  його, не переписує original runtime report заднім числом.
- Повернено ORIGINAL secure6213983/Hybrid (`u 1`): cardwired1294625,
  invocation `a6c1434fdeb64fb4bce9643a2a0d2c97`. KWin187650/реальний187661
  invocation `ff4db9d153cd4c159296be85edcaa666`, llama5151
  invocation `efe4e0b1876c4393ad62c023d1ebbe12` незмінні. Таймеріввідкату немає.
- Candidate журнал містить правильні host BTF Task10240/2888/2876 та File560/296
  offsets; у переглянутому вікні немає `could not read`. Залишаються повідомлення
  відсутніх optional NVIDIA hwmon/backlight nodes та EGL GPU-type query, тому
  НЕ називати весь daemon log «безпомилковим».
- Scope: коротка ізоляція НОВИХ тестових процесів + CUDA discovery + explicit
  AMD graphics і видимий куб. НЕ persistent policy, не real-app/Flatpak/Electron,
  не inference restart, не CUDA workload benchmark, не lifetime/PID reuse proof,
  не Work/iGPU фізичний тест і не security boundary проти FD inheritance.
  Не повторювати короткий тест без причини. Наступний змістовний етап: стійкі
  вузькі service/compositor permissions на exec/start/restart Cardwire, а не
  ще один разовий allowlist. Host permanent install/logout потребують згоди.

## Продовження 2026-10-04: archive guard виправлено для timer unit

Користувацький `--archive-restored && --start` зупинився на перевірці timer;
жодного нового host candidate/Smart запуску не було (outer invocation досі
`a33a274228d642f29a053992b9e324c2`, daemon1257706, Hybrid).
Фактичний timer: `LoadState=not-found`, `ActiveState=inactive`, `SubState=dead`,
`Job=`; `MainPID`/`ControlPID` для timer взагалі не повертаються. Наш guard
помилково вимагав service-specific поля від timer. Архівування ще не робило rename.

`quiescent_unit` тепер запитує поля за типом unit: timer — load/active/sub/job;
service — також обов'язкові нульові MainPID/ControlPID. Unknown/masked,
active/queued/incomplete states відхиляються; безпосередню перевірку idle не прибрано.
Offline: candidate16 + smart52 =68 PASS. Додатково саме виправлена функція
read-only успішно перевірила всі три РЕАЛЬНІ host units. Старі артефакти
не змінювалися, services/mode не перезапускалися. Можна повторити той самий
явний користувацький блок `--archive-restored && --start`.

## Продовження 2026-10-04 09:22: host candidate стартував; Smart guard зупинив неправильні bind mounts

Користувач виконав запропонований `sudo ...test-cardwire-exact-candidate.py --start`.
Оригінальний pkexec з попереднього запису був скасований; це новий фактичний запуск.

- Transient service invocation `a33a274228d642f29a053992b9e324c2`, main1256525,
  09:22:48–09:22:54 EEST. Candidate запущено й checksum/Hybrid пройшли.
  Inner helper1256840 відмовив ДО Smart: `Cardwire is not using private test config/state`.
  Це НЕ proof Smart enforcement/rendering і не NVIDIA hardware failure.
- Inner rollback пройшов; outer ExecStopPost1257478 exit0 підтвердив повернення
  secure6213983/Hybrid та незмінні KWin/llama. Host readback:
  cardwired1257706/invocation102b82f6665042a7b3969e6d7a972a96, Mode `u 1`;
  KWin187650/invocationff4db9d153cd4c159296be85edcaa666,
  llama5151/invocationefe4e0b1876c4393ad62c023d1ebbe12. Rollback timers відсутні.
- Root cause нашого wrapper: `98-exact-candidate-test.conf` має порожній
  `BindReadOnlyPaths=`, що за systemd.exec скидає ОБИДВА bind списки, включно
  з private config/state у попередньому `91-egpu-smart-test.conf`.
  Старий unit test помилково перевіряв лише текст, а не merge semantics.
- SOURCE FIX: лише exact trial використовує `99-egpu-smart-test.conf` після98.
  Старі legacy trial/watchdog зберігають91. Staged exact watchdog правильно
  відновлює99. Перевірку samefile/unique owner НЕ прибрано й не послаблено.
- Wrapper має окремий `--archive-restored`: лише після original baseline,
  root-owned restored marker, quiescent worker/watchdog і відсутності91/98/99
  overrides зберігає обидва trial каталоги в root-private recoverable archive.
  Не рестартує сервіси/не змінює GPU policy; reset-failed лише завершеного
  transient unit дає змогу наступного явного запуску.
- Offline suites: candidate12 + smart52 =64 passed, diff check passed.
- Додатковий real-systemd guest regression: session24000 terminal exit0,19.32s.
  `OLD_ORDER_PRIVATE_BIND_RESET_REPRODUCED` і `NEW_ORDER_PRIVATE_BINDS_PRESERVED`.
  Лише disposable fixture unit у rootless VM (навіть не запускався), не host
  daemon/GPU trial. Evidence `/work/bind-reset-vm-result` у
  `cardwire-policy-vm-build-20261004`; script та fixtures
  `/tmp/cardwire-parent-vm.Rm4N5B/bind-reset-*` + guest-container copies.
  Перша invocation мала лише missing output directory error до старту VM;
  після створення каталогу реальний regression завершився успішно.
- Повторний HOST Smart test після source fix НЕ запускався. Користувачу можна
  запропонувати послідовно `--archive-restored`, потім `--start` на repo wrapper.
  Не редагувати стару staged runtime copy: її початковий failure/rollback evidence
  треба зберегти. Системних налаштувань у цьому діагностичному turn не змінено.

## Продовження 2026-10-04: нативний candidate і новий дозволений короткий тест

Цей запис уточнює попередню відсутність дозволу: користувач відповів `OK` на
повторний короткий Smart → Hybrid тест без розлогіну та restart llama.
Стан до запуску: secure6213983 PID331019, Hybrid, KWin MainPID187650 та
llama.service MainPID5151; invocation IDs збігаються з попереднім записом.

- Native full build Rust1.95/nightly2026-08-12 завершився. Artifact (не release):
  `cardwire-stable-process-access/dist/local-exact-btf-2e63169e3ff064cb/`.
  Daemon SHA256 `57b495249ae2959f15c79b41183533ee8299b88eda43f68a2ef952f2772039bd`.
  Source manifest SHA256 `2e63169e3ff064cbfbcc6b502f014ea26cc72151f5d5b5b6e23b0c8f749dd6`.
- Саме ці native daemon bytes пройшли unchanged full 2-GPU VM suite
  (exec42177 exit0,25.43s), explicit Nix loader/libraries, без ELF patching.
  Native GUI не запускався. Source/Nix 3/15-GPU gates описані нижче.
  Evidence: `/tmp/cardwire-parent-vm.Rm4N5B/native-vm-explicit-loader.log`.
- BUILD.json metadata і checksum оновлені; всі6 SHA256SUMS entries PASS.
- Smart helper має окремий `--apply-exact-amd-render`, тільки pinned candidate
  checksum, `Allow_dGPU_Exact` для чинних GPU clients і обов'язковий readback
  `AllowedExact`. Inheritable fallback заборонено. Сесійні launcher-и виключені.
  Це НЕ persistent profiles/lifecycle grants або FD containment.
- Новий `diagnostics/test-cardwire-exact-candidate.py --start` робить лише
  тимчасову runtime binary підміну. systemd-owned worker має RuntimeMaxSec180s
  і ExecStopPost відновлення попереднього secure6213983 та original Hybrid.
  Inner policy watchdog120s збережено. Немає RPM install, permanent override
  rewrite, KWin/llama restart, logout, PCI/kernel changes чи commit/push.
- Offline: smart50, AMD57, candidate8 tests PASS. Wrapper pinned machine-specific
  diagnostic, не universal installer. Root runtime `/run/egpu-cardwire-exact-candidate-test`.
- `pkexec ... --start` не отримав автентифікацію: після повторних перевірок
  service залишався `LoadState=not-found`, журнал порожній. Щоб уникнути
  несподіваного пізнього запуску, саме pending pkexec PID689521 скасовано
  SIGTERM. Session11269 terminal exit143; інших запусків немає.
- Після скасування підтверджено: Cardwire331019/invocation1fb1f01f5ee24cf98268efe2f5782b16,
  Hybrid (`u 1`), KWin187650/invocationff4db9d153cd4c159296be85edcaa666,
  llama5151/invocationefe4e0b1876c4393ad62c023d1ebbe12 — незмінні.
  Live candidate/Smart test НЕ виконаний, runtime deployment service не створено.
  Наступний крок потребує автентифікації користувача: `sudo python3
  /var/home/keefeere/_repos/_home/bazzite-th5p4-nvidia-egpu/diagnostics/test-cardwire-exact-candidate.py --start`.
  Не запускати повторно через автоматичне продовження. Попередній pending
  запит закрито, тому ця команда не дублюватиме його.
- Перед тестом RTX idle sample: 0% GPU,10594MiB,22.09W — не доказ виправлення
  high-load симптомів. Мета трьох профілів не виконана; потрібні host proof,
  persistent lifecycle grants, реальні програми/ізоляція, Work/iGPU і regression.

## Продовження 2026-10-04: FileLayout candidate — unit gates і вся VM matrix PASSED

Цей запис уточнює старі записи нижче. Host Cardwire досі secure6213983,
Hybrid (`u 1`), PID331019. KWin/llama invocation та PID не змінено. Новий
Smart test/deployment/logout/reboot НЕ виконувалися й потребують нового дозволу.

- Runtime BTF resolver тепер охоплює потрібні поля `inode`, `dentry`, `file`,
  `path`, із типами покажчиків, anonymous/named union, bounded recursion/work,
  alignment/overlap/bounds validation. `CW_FILE_LAYOUT` заповнюється до hooks.
  File/inode hooks використовують probe reads замість generated dereferences.
- Untracked resolver перейменовано на
  `crates/cardwire-ebpf-userspace/src/kernel_layout.rs`; task_layout.rs у repo
  більше немає. Включати НОВУ назву в snapshots/commit. SHA-256:
  `e90a833633dd71d3c7067000da7ba47840f651850d782f5cc12acb279f8c5520`.
- Native exec66825 exit0: **178 tests passed** (16 CLI +94 daemon +31 userspace
  +26 GUI +11 policy). Exec49938 exit0: full all-target/all-feature clippy
  `-D warnings`, pinned rustfmt check passed. Це включає нові file-layout tests.
- Read-only native BTF diagnostic exec48458 exit0: host FileLayout inode560,
  ino64, alias296, dentry192/inode48/name40/alias176, file176/dentry72,
  path16/dentry8. Task size10240/parent2888/tgid2876. Не створював BPF maps чи
  hooks. Example переміщено за межі crate в `/build/runtime-layout-read-diagnostic.rs`.
- VM regression тепер перевіряє `os.access(R_OK, effective_ids=True)` без
  file_open поряд із actual opens, exact inheritance, root auth і320writes.
  У всіх трьох VM suites додано journal gate проти `could not read` errors.
- Frozen `/work/source` у rootless `cardwire-policy-vm-build-20261004` звірено
  по SHA-256: усі tracked files +kernel_layout.rs. Старий snapshot task_layout
  збережено поза source в `/work/task-layout-before-file-fix.rs`.
  Repo tracked diff SHA-256:
  `10407280ccc2b9ddeeb7bbab73956134df6c644d4e2211e4fa45f012616fa73d`.
- Exec66057 зібрав full Nix package за11m32s:
  `/nix/store/6861frpdqrkyfhhmmzpavwp4arg4wqnr-cardwire-0.12.3`.
  Exit1 лише через Cargo-created `/homeless-shelter` перед наступним derivation.
  Після terminal і перевірки відсутності builders60KiB збережено в
  `/work/files-package-home.AgAF4r/home`.
- **Exec48350 exit0: full2GPU PASSED**,27.11s. Actual opens, permission-only
  access, root-onlyAPI, exact-vs-inherited та320writes пройшли. GuestBTF:
  inode608/alias304, file184/dentry72. Новий journal gate passed: жодного
  `could not read` у журналі Cardwire до завершення тестових операцій.
- **Exec20997 exit0: full15GPU і3GPU PASSED**,21.55s/12.79s, включно з
  journal gate та рестартами Cardwire у guest. Це НЕ host NVIDIA validation.
  Cached outputs (scoped eval exec48368 exit0):
  -2GPU `/nix/store/1g2cvz5nmn7zlms5qp04q4f4p152dmg6-vm-test-run-cardwire-test`
  -3GPU `/nix/store/s5k83dv62xp943l0rjpk07dqynd4bdg5-vm-test-run-cardwire-test`
  -15GPU `/nix/store/wdwfjydsksna8n3csx2yf5ydvg0gh23f-vm-test-run-cardwire-test`
- **Negative control exec63260 exit0**,14.14s: archived task-only package
  `/nix/store/ljxn08qs77sz4dw30npfjq0yqyd286sr-cardwire-0.12.3` у старій VM,
  власний probe PID з Force_GPU=0: `device_open=[true,false]`, але
  `permission_only=[true,true]`. Отже regression дійсно відтворює old defect,
  а не лише додає завжди-зелений тест. Diagnostic у
  `/tmp/cardwire-parent-vm.Rm4N5B/negative-control-inode-vm.py`, копія
  `/work/parent-diagnostic/negative-control-inode-vm.py`. НЕ production script.
- Broad eval усіх Nix checks (exec49186) помилково зачепив precommit toolchain
  й завершився через відсутню nixbld group; це diagnostic invocation error,
  не failure VM matrix. Scoped eval з потрібною build-users-group passed.
- **Усі handles вище terminal.** Native example та archived layout файли
  поза crate не включати в майбутній artifact. Host Hybrid/KWin/llama знову
  read-only перевірено; PID/invocation без змін. Поточний short Smart дозвіл
  уже використано, не повторювати live transition за ним.
- Жодного commit/push/deployment цієї candidate. Lifecycle grants, реальні
  applications, Work/iGPU, rollback/UI та повна мета ще не завершені.

## Продовження 2026-10-04: runtime task layout пройшов VM matrix; окремий inode layout defect

Цей запис замінює попередню позначку «ще не реалізовано» нижче. Host deployment
та новий live Smart test НЕ виконувалися. Поточний короткий дозвіл уже використано.

- У Cardwire source `task_struct` parent/TGID offsets тепер читаються з BTF
  поточного ядра, перевіряються типи, межі, вирівнювання, aliases/цикли та
  неоднозначність. `CW_TASK_LAYOUT` заповнюється до першого attach. Helper
  використовує probe reads і звіряє власний TGID з kernel helper. Статичного
  fallback немає; інші inode/dentry accesses не змінені, це НЕ повний CO-RE.
- Нова `crates/cardwire-ebpf-userspace/src/task_layout.rs` поки untracked;
  обов'язково включати її в майбутні snapshots/commit. SHA-256:
  `55eba76456a8d3e5d09488c730d39aac780e04eba804744e28b8613c2d2c1b4d`.
- Pinned Rust 1.95/nightly-2026-08-12: **129 scoped tests passed**
  (94 daemon +25 userspace +10 policy), clippy з `-D warnings` і fmt check
  passed. Відновлено результат read-only прикладу: поточне host BTF має
  `TaskLayout { size: 10240, real_parent: 2888, tgid: 2876 }`. Жодного eBPF
  attach на host цим diagnostic не було. Тимчасовий example переміщено з
  crate у `/build/task-layout-read-diagnostic.rs` після завершення.
- VM source `/work/source` у `cardwire-policy-vm-build-20261004` тепер
  відповідає всім tracked repo files + новій task_layout.rs (повний SHA-256
  manifest comparison passed). Repo tracked diff SHA-256:
  `9cf33b22913fed77708a839cac571bb3f2200be3e4e2f8a9dd1f498fccec1779`.
  Не змінювати snapshot під час build. Процесні VM assertions лише отримали
  actual/expected/PID у помилках; жоден gate/expected не послаблено.
- Exec 53360 зібрав full Nix package за 12m25s:
  `/nix/store/ljxn08qs77sz4dw30npfjq0yqyd286sr-cardwire-0.12.3`.
  Exit 1 лише після build через `/homeless-shelter`. Після terminal і перевірки
  відсутності builders 60 KiB каталог збережено у `/work/btf-package-home.wZLxmo/home`.
  Exec 54754 завершив повний native workspace `cargo +1.95.0 test --locked`
  у `cardwire-pr-validation`, source `/build/exact-policy.ZGkn6H`.
  **171 tests passed** (16 CLI +94 daemon +25 userspace +26 GUI +10 policy).
  Exec 75061 також exit 0: full `clippy --all-targets --all-features -- -D warnings`.
  Усі Rust/Cargo source files звірені SHA-256 із repo.
  **Exec 62524 exit 0: повна 2-GPU VM passed**, 24.09s. Guest BTF task layout
  size4032/parent1936/tgid1924. Positive legacy parent Allow, negative Exact,
  root-only authorization, 320 concurrent writes і всі наступні gates пройшли.
  **Exec 67186 exit 0: 3-GPU та 15-GPU suites passed**, 12.97s /15.20s.
  Це virtual GPU kernel-enforcement evidence, не host NVIDIA/USB4 validation.
  Container rootless 2 CPU/3 GiB, тільки `/dev/kvm`, немає фізичних GPU чи
  host system bus. OOM kill count=1 лише зі старішої fat-LTO збірки.
- Read-only helper audit тепер ловить зміну executable/UID/cgroup між двома
  читаннями, навіть якщо PID/start_ticks не змінився. Це все ще snapshot,
  НЕ атомарна ідентифікація і НЕ grant/reconciliation engine. Чотири нові
  synthetic tests; helper suites **55+11+44+57=167 passed** (exec 85714 exit 0).
- Host знову підтверджено Hybrid (`u 1`), cardwired PID331019; KWin MainPID187650
  invocation `ff4db9d153cd4c159296be85edcaa666`, llama.service MainPID5151
  invocation `efe4e0b1876c4393ad62c023d1ebbe12`. Сесію/llama не перезапускали.
  Read-only sample 02:58:43: RTX 0% GPU, 1% memory utilization, 10594 MiB,
  19.43 W; pmon за 5 секунд не показав SM activity. KWin supportInformation
  досі AMD Radeon 890M, Mesa 26.2.2. Це idle sample, НЕ доказ виправлення
  попередніх 100% і не контрольований performance-тест активних дисплеїв.

У VM logs попри PASS знайдено **1119 inode_permission probe errors** у 2-GPU
suite, ще40 у 3+15. Вони не заміняють результати actual device-open tests і
не приховані/вимкнені. Додаткова read-only VM діагностика exec48645 exit0
підтвердила другу static-layout залежність (не зміна production коду):

- Generated Rust inode size568, `i_dentry.first=296`, `i_ino=64`.
- Guest Linux6.18.46 BTF inode size608, `i_dentry.first=304`, `i_ino=64`.
- Host Linux7.2.7 BTF inode size560, `i_dentry.first=296`, `i_ino=64`.
- В усіх трьох dentry: size192, d_inode48, d_name.name40, d_alias176;
  file f_path.dentry72; path.dentry8. Це саме потрібні коду поля, не повний
  доказ переносності всіх структур/режимів/архітектур.
- **Embedded eBPF саме BTF-candidate Nix binary**: lsm/inode_permission
  instruction472 `LDX_DW dst7 src1 offset296`, наступне subtract176. Отже
  inode alias lookup справді читає старе поле, попри guest offset304.
  Це не доводить причину host RTX load; на host саме це поле збігається.
- Diagnostic scripts у `/tmp/cardwire-parent-vm.Rm4N5B/` та
  `/work/parent-diagnostic/`: read-inode-btf.py, diagnose-inode-vm.py,
  read-parent-insns.py (тепер підтримує inode_permission section).
  Native layout example після cargo run переміщено з examples у
  `/build/inode-layout-cargo-diagnostic.rs`. Standalone rustc attempt раніше
  не мав aya dependency та впав; це diagnostic invocation error, НЕ failure
  Cardwire build/tests. Усі ці jobs terminal. New driver:
  `/nix/store/0fl6g0357734i5wr4avplyjdvqakkmml-nixos-test-driver-cardwire-test`.

Далі безпечний source-only крок: прибрати static inode-alias offset залежність
за тим же принципом validated BTF layout, не змінювати host kernel і не
просто вимикати error logging/hook. Потрібно додати regression gate на цю
помилку та зберегти exact/legacy actual-open tests. Усі поточні VM/build jobs
terminal; не запускати старі handles повторно. Жодного нового host deployment,
Smart switch, logout або llama restart без окремої згоди. Lifecycle grants,
реальні applications, Work/iGPU, UI/rollback і повна мета досі не завершені.

## Продовження 2026-10-04: verifier виправлено, VM довела неправильний parent task layout

**Авторитетний поточний стан: усі VM/build handles нижче terminal. Новий
Exact candidate НЕ готовий до встановлення. Не запускати live Smart або
deployment повторно за старою згодою.** Повторний короткий host Smart test
раніше вже завершився; visual confirmation лишається невідомим.

- Exec 12113 повністю зібрав scalar candidate за 17m44s. Package:
  `/nix/store/692r8zjk1bsbgrd5j2jr2g2vxdqnlz72-cardwire-0.12.3` усередині
  `cardwire-policy-vm-build-20261004`. Exit 1 лише перед наступним derivation
  через Cargo-created `/homeless-shelter`. Після перевірки відсутності живих
  builders каталог 60 KiB збережено у `/work/scalar-package-home.d7UspJ/home`.
- Exec 78677 запустив цей cached package у повній 2-GPU VM: **eBPF verifier
  прийняв програму, daemon працює**, API/root authorization і deterministic
  own Allow/Exact/Force перевірки пройшли до parent inheritance assertion.
  Потім `nix/process-policy-test.py:150` впав на **legacy Allow_dGPU child**.
  До 320 concurrent writes і наступних suite tests цей запуск НЕ дійшов.
  Failed gate drv: `/nix/store/68v9jqq6c9gczv9cbvi2ms662jyldabg-vm-test-run-cardwire-test.drv`.
  Driver: `/nix/store/173mcxqkqmsmj4fixkwdsdxl00qy5hv3-nixos-test-driver-cardwire-test/bin/nixos-test-driver`.
  Config: `/nix/store/hwsghk7f99lsr90m5hkglf6gd0d74p3w-driverConfiguration.json`.
- Додаткова діагностика exec 57508 (exit 1 очікуваний від reproducer;
  попередня 91743 лише argument error через відсутню output dir) використала
  **той самий cached VM/package**, НЕ підмінила тестову логіку на weaker gate.
  Лише додала actual/expected до assertion і прочитала guest BTF. Результат:
  `('Allow_dGPU', [True, False], [True, True], ['Allowed', [0]], 737, 732)`.
  GPU1 child open справді blocked попри Allowed у parent.
- Підтверджений layout mismatch:
  - repo-generated `vmlinux::task_struct`: size=6144, real_parent=2944,
    tgid=2932, group_leader=2992 (native Rust offset_of diagnostic, exec 49600).
  - guest Linux 6.18.46 BTF: size=4032, real_parent=1936, tgid=1924,
    group_leader=1984.
  `get_task_ppid()` у helpers.rs використовує саме ці статичні Rust offsets.
  Додатково прочитано **embedded eBPF саме Nix package** (не лише native
  layout example): у `lsm/file_open` після helper 35 (`get_current_task`)
  instructions 1625/1687 додають `2944`, instructions 1634/1696 додають
  `2932` перед helper 113 (`probe_read_kernel`). Отже це підтверджено й для
  фактичного binary, на якому впав VM gate. Read-only extractor:
  `/work/parent-diagnostic/read-parent-insns.py` (host copy у тому ж tmpdir).
  У generated struct також є BORE-specific поле; не узагальнювати до всіх
  host симптомів без окремого доказу. Exact scalar patch не виправляє цей
  старіший structural defect. Тест не можна пропустити або змінити expected
  на blocked, бо це стерло б перевірку збереження legacy Allow.
- Діагностичні scripts: host `/tmp/cardwire-parent-vm.Rm4N5B/`, container
  `/work/parent-diagnostic/`. `read-task-btf.py` читає raw BTF/ELF; у native
  eBPF object's BTF task_struct не знайдений, тому compiled offsets отримані
  окремим native example. Цей example після виконання переміщено з crate
  examples у `/build/parent-diagnostic-task-offsets.rs`, щоб не псувати
  майбутній all-targets clippy 3023 generated-binding warnings. Host repo
  example НЕ додавали. Жоден diagnostic не запускав BPF на хості.
- 3/15 GPU gates ще **не запущені** для цього candidate. Source diff SHA
  у repo досі `eeb6330f40976978a1d2e4f3b0bd52048f859d034ba56179a306541a8e2c7787`;
  VM frozen diff `be5f614762439b632b550a23003652dbbdb0d990415a42f502f496def5f979dd`
  відрізняється лише двома переносами. 103 Rust tests / clippy / formatting
  passed. Повторні helper tests: **51+11+44+57=163 passed** (запускати файли
  напряму, unittest discover не імпортує hyphenated filenames).

Наступний безпечний крок: виправити kernel-layout dependence parent lookup
у Cardwire source, **не** регенерувати static vmlinux лише під один guest і
не чіпати host kernel. Кандидат: отримати real_parent/tgid offsets із BTF
поточного ядра в loader, перевірити типи/межі/вирівнювання, заповнити малу
layout map **до attach** LSM/tracepoints; helper читає через probe_read_kernel
за цими offsets. Missing/ambiguous/unsupported layout — явна відмова, не
hardcoded fallback. Поки це лише напрямок, нічого такого ще не реалізовано.
У pinned Aya 0.14 / aya-obj 0.3 public є `Btf::from_sys_fs`, `to_bytes` та
`id_by_type_name_kind`, але `type_by_id`, Struct.members/size/string_at —
**pub(crate)**; не вигадувати public API. Приватний одноразовий Python parser
не переносити без bounds/type/fixture tests у production loader. Альтернатива
CO-RE потребує окремої перевірки підтримки нашим Rust/eBPF toolchain.

Після виправлення повторити positive legacy inheritance + negative Exact,
повну 2/3/15-GPU matrix, лише потім готувати maintained host artifact і
просити новий вузький дозвіл deployment. Persistent lifecycle reconciliation,
реальні desktop apps, Work/iGPU, performance та profile activation все ще
не завершені. На хості Hybrid, той самий KWin/llama; rollback timers від
минулого тесту не перезапускались. Commit/push/deployment не виконувалися.

## Продовження 2026-10-04 02:06: VM відхилила Exact, scalar candidate на повторній збірці

Цей запис замінює позначки live build нижче. Нових host mode switches,
deployment, service/session restart, commit/push НЕ було.

- Exec 9076 завершив повну Nix package compilation/install (daemon, CLI, GUI),
  але наступний derivation спіткнувся об `/homeless-shelter`. Після завершення
  build цей Cargo-only каталог збережено у
  `/work/completed-package-home.NZg0mH/home`, нічого не видалено.
- Exec 93121 повторно використав пакет і справді запустив 2-GPU VM. Її
  Linux 6.18.46 відхилив eBPF `file_open`: `R1 !read_ok` на spill інструкції
  `*(u64 *)(r10 -208) = r1` у гілці `ProcessPolicy::smart` із Exact. Це
  **реальний failed verifier gate**, не VM pass і не host permissions issue.
  Ймовірна причина — lowering невизначеного enum payload; не доведений
  загальний Rust/compiler bug. Nix PID 37439 перервано SIGINT після збору
  помилки. Exec 93121 terminal exit 1; QEMU/test driver залишились лише
  zombies, живого guest немає. Попередні handles 95298/59224 теж terminal.
- У `cardwire-policy` додано scalar `smart_encoded`/`manual_encoded`:
  initialized u64 на кожній гілці, `u64::MAX` для absent/invalid, Exact
  локальний, legacy parent-Allow і Force precedence незмінні. eBPF helper
  більше не декодує/зливає `Option<ProcessPolicy>` payload у kernel path.
  Userspace API/status/encoding не змінені. Додано 144-pair raw matrix
  (також invalid encodings і u32 boundaries) проти typed reference.
- У pinned build container **94 daemon + 9 policy tests passed**, scoped
  clippy з `-D warnings` passed; eBPF compiled. Це НЕ verifier evidence.
  Nightly format check спочатку знайшов два зайві переноси для `let parent`
  у helpers.rs. Виправлено лише у repo/pinned-test snapshot; fmt check та
  повторні 103 tests/clippy passed. Заморожений VM snapshot не міняли:
  різниця з repo — лише ці два whitespace-only переноси, логіка ідентична.
  Поточний repo diff SHA-256:
  `eeb6330f40976978a1d2e4f3b0bd52048f859d034ba56179a306541a8e2c7787`.
- **Поточний exec 12113 live**: повна повторна Nix package + 2-GPU gate
  у `cardwire-policy-vm-build-20261004`, Nix PID 38732. Source snapshot
  HEAD 6213983 + сім tracked diffs, `git diff --binary HEAD` SHA-256
  `be5f614762439b632b550a23003652dbbdb0d990415a42f502f496def5f979dd`.
  Не запускати дубль. Контейнер rootless, 2 CPU / 3 GiB, лише /dev/kvm,
  фізичні GPU та system bus не передані. OOM count досі 1 зі старої збірки.
  3/15-GPU matrix і Exact host test ще не пройдені.

Read-only host snapshot: Hybrid (`u 1`), cardwired PID 331019, KWin wrapper
187650/actual 187661 з тією ж invocation. **llama.service** (не
llama-server.service) active, MainPID 5151/worker 17712, та ж invocation
`efe4e0b1876c4393ad62c023d1ebbe12`. NVIDIA high utilization зберігається:
один pmon sample показав ~49% і для llama, і для KWin; це лише sample, не
доказ причинності або розподілу загального GPU load. Python GPU client
207689 — існуючий Gear Lever Flatpak, не забутий наш benchmark. Нічого
із цих процесів не завершували. Новий visual confirmation ще не отримано.

## Продовження 2026-10-04 01:46: вузькі Work-винятки та повторна VM-збірка

Попередній goal turn дав новий live-доказ (Smart test exit 0 з rollback),
тобто це progress. Візуального підтвердження користувача на новий куб ще
немає; автоматичне продовження НЕ замінює його і не дає нового live-дозволу.
На цьому кроці змінено лише джерела/тести та ізольовану збірку.

- У planner прибрано небезпечний майбутній Work compositor hint
  `CARDWIRE_ALLOW=1`: для обох Work тепер `0`. Work/NVIDIA потребує окремого
  перевіреного `Allow_dGPU_Exact` на фактичний compositor, не wrapper/unit.
  Display/driver exception candidates явно non-inheritable. Gaming candidate
  і compute hint не змінені. Активації досі немає; startup/exec/restart grant
  reconciliation ще потрібно реалізувати й довести перед увімкненням профілів.
- `diagnostics/egpu-cardwire-api-probe.py --exact-policy` підготовлено для
  наступного погодженого deployment test: 52 fresh/repeated/replacement
  API+readback checks на власних disposable sleep children в Hybrid. Old daemon
  rejection або silently inheritable Allowed замість AllowedExact = failure,
  fallback немає. Без прапорця лишаються колишні 21 checks. Report окремо
  позначає `inheritance_enforcement_verified=false`: Hybrid API readback НЕ
  заміняє Smart kernel enforcement. На хості цей новий probe НЕ запускали.
- **163 offline tests passed**: 51 planner, 44 Smart, 11 API, 57 KWin.
  Python syntax і diff whitespace checks пройшли.

VM gate:

- Exec 95298 завершився **exit 1**, GUI rustc отримав deadly signal. У
  контейнері `memory.events` показав `oom_kill 1`, `memory.peak` ~3 GiB при
  `memory.max=3221225472`: підтверджено resource failure збірки, VM не стартувала.
- `cardwire-stable-process-access/nix/default.nix` тепер додає
  `cargoBuildFlags = [ "--config" "profile.release.lto=false" ];`, як уже
  робить основна release-artifact CI job. Не виключено GUI/CLI/daemon або
  жоден тест. Ліміти host/container пам'яті не збільшено.
- Лише цей Nix file скопійований у попередній container snapshot. Snapshot
  тепер HEAD 6213983 + шість tracked diffs; `git diff --binary HEAD` SHA-256:
  `87c26a898867e40de8773bb4cd7e1486a6ddb49fef4d8f55b2bd761268427483`.
- Exec 59224 був terminal exit 1 до compilation: Nix відмовився через
  `/homeless-shelter`, залишений Cargo після failed build. Перевірено: там
  лише 60 KiB Cargo cache; у контейнері не лишилося compiler/build процесів.
  Directory збережено (НЕ видалено) у `/work/failed-build-home.TzVyy2/home`.
- **Поточний exec 9076 live**, Nix PID 24843 всередині
  `cardwire-policy-vm-build-20261004`. Підтверджений cargo invocation містить
  `--config profile.release.lto=false`; на останньому poll ще компілювалися
  GUI dependencies, пам'ять ~2.26 GB, `oom_kill` досі 1 (старий випадок).
  Не запускати дубль. Poll 9076; при втраті handle перевірити `podman top`.
  Нового VM result немає, 3/15-GPU matrix ще попереду.

На хості Mode=Hybrid, KWin wrapper 187650/invocation
`ff4db9d153cd4c159296be85edcaa666`, llama 5151/invocation
`efe4e0b1876c4393ad62c023d1ebbe12` незмінні. Нового deployment, service restart,
logout, commit/push, hardware transition не було. Exact binary досі НЕ installed.

## Продовження 2026-10-04 01:38: погоджений повторний Smart-тест пройшов технічні перевірки

Користувач відповів «OK» на конкретний запит повторити короткий Smart-тест
із restart лише Cardwire, без logout/llama restart, з поверненням Hybrid.
Це замінює попередній статус «live-дозволу ще немає» нижче, але НЕ дозволяє
встановити новий exact-policy binary або запускати ще один session transition.

Перед запуском додано й offline перевірено `--archive-amd-render`:
root-owned restored trial, відсутній private override, обидва rollback units
inactive/без jobs, незмінні Hybrid/config/files/daemon digest. Лише після
цих перевірок стару спробу збережено у
`/run/egpu-cardwire-smart-amd-render-test-archive-oi4eym9p/trial`.
Архівування НЕ змінювало політику або services. Нові archive guard tests
перевіряють також stale timer/job/PID, чужий override/symlink і змінений baseline.
Offline: **44 Smart + 57 KWin + 49 planner + 7 API = 157 passed**, diff check OK.

Один live `--apply-amd-render`, exec 99860, завершився exit 0. Артефакт:
`/run/egpu-cardwire-smart-amd-render-test/result.json`.

- Нові ungranted UID 1000 і UID 0 процеси не відкрили жоден із чотирьох
  перевірених NVIDIA nodes; AMD render node доступний обом.
- Новий allowed user probe відкрив NVIDIA nodes і успішно виконав CUDA
  initialization/device enumeration: одна GPU. Це НЕ inference benchmark.
- GLX, Wayland EGL та vkcube: exit 0, фактичні AMD hardware renderers,
  software fallback/render errors не виявлені.
- Live KWin report під Smart показав незмінний AMD renderer та обидва
  enabled NVIDIA outputs DP-7/HDMI-A-1. Після повернення Hybrid перевірено
  той самий DRM mapping/EDID і ту саму process identity KWin PID 187661.
- Original Cardwire config/state hashes перевірені restore helper;
  повернено Hybrid (`u 1`), новий cardwired PID 331019 active/running.
  Rollback timer і service inactive, pending jobs немає.
- User KWin unit invocation `ff4db9d153cd4c159296be85edcaa666`, wrapper
  PID 187650, та user llama invocation `efe4e0b1876c4393ad62c023d1ebbe12`,
  PID 5151, до/після незмінні. Немає logout, llama restart чи deployment.

Статус `device-access-passed-rendering-needs-visual-confirmation`,
`visible_output_verified=false`, `applied_profile=null`. Поставлено питання
користувачу про куб та обидва монітори; відповіді на момент запису ще немає.
Попереднє «куб був» стосувалося іншої спроби й тут не використане.
14 існуючих GPU clients тимчасово отримували legacy grandfather grants:
це обмежений diagnostic, НЕ готова Work-ізоляція зі службовими винятками.
Проблему високого RTX навантаження цей pass НЕ пояснює й НЕ виправляє.

Окрема VM-збірка exact-policy досі працює в exec 95298 (на останньому poll
компілювалися cardwire-daemon/cardwire-gui). VM pass ще НЕ отримано;
не запускати дубль, не встановлювати цей build на хост.

## Продовження 2026-10-04: локальна VM-збірка exact-policy, live-дозволу ще немає

Запит на повторний короткий Smart-тест після виправлення observer лишається
без відповіді. Автоматичне продовження goal НЕ трактувалося як згода: GPU
mode, Cardwire, KWin, llama та runtime trial state на хості не змінювалися.

Натомість запущено окремий обов'язковий VM gate exact-policy патча:

- Source: `cardwire-stable-process-access`, HEAD
  `62139830260839c037c97e6a8dc607cc47559b90` + п'ять поточних tracked diffs.
  SHA-256 `git diff --binary HEAD`:
  `77c2ecb7285aa4631dfa558092ce24580f6f56ad1f4d024d0a1c345276d60bb6`.
  Через `git ls-files` створено snapshot `/work/source` у тестовому контейнері;
  наступні зміни репо автоматично до цього запуску НЕ потраплять.
- Старий Nix 2.8 у build container був реально перевірений: pinned nixpkgs
  відмовився від evaluation, потрібен >=2.18. Лише всередині контейнера
  встановлено Nix 2.35.2 з official binary archive, SHA-256
  `0c3960a9792331a22081c3c7a5d8465db9b17c50b3acdf18587fa4c6f2cb1158`
  взято з `https://nixos.org/nix/install`. Host packages/profiles не змінені.
- Rootless container `cardwire-policy-vm-20261004` з cap-drop=ALL завершив
  першу спробу помилкою tar/chown під час unpack dependency. Це НЕ failure
  exact-policy test: VM ще не запускалася. Exec 84725 terminal exit 1.
- Його кеш збережено local-only image
  `localhost/cardwire-policy-vm-runtime:20261004`; новий rootless container
  `cardwire-policy-vm-build-20261004` використовує звичайні контейнерні caps,
  НЕ privileged. Limits: 2 CPU, 3 GiB memory+swap total, 1024 PIDs.
  Передано лише `/dev/kvm`; фактично перевірено відсутність `/dev/dri`,
  `/dev/nvidia0`, `/run/dbus/system_bus_socket` і host mounts. GPU лише virtio
  всередині майбутнього QEMU guest, не фізичні AMD/NVIDIA.
- **Поточний exec 95298 був live на останній перевірці. Не запускати дубль.**
  Він виконує `nix build --no-link --print-build-logs --no-write-lock-file
  path:/work/source#checks.x86_64-linux.vm-ci-2gpu` із sandbox=false тільки
  всередині контейнера, max-jobs=1, cores=2, порожньою build-users-group.
  Nix binary:
  `/nix/store/irfrbndi76zhkvqsfhmsn4a99iafck29-nix-2.35.2/bin/nix`.
  Залежності вже розпаковані; почалася unpackPhase самого Cardwire. Pass ще
  НЕ отримано. Спочатку poll цього handle; якщо missing — перевірити живий
  Nix/QEMU у `podman top`, а не повторювати build лише через втрату handle.
- У build container PID1=sleep, помічено 150 unreaped helper zombies при
  174/1024 tasks; це не зависання Nix. Для майбутніх контейнерів використати
  `--init`; поточний active build не перезапускати без конкретної причини.
  Завершення цього контейнера після тесту прибере orphan processes; cache
  лишається. Нічого не видалено і image нікуди не публікувався.

2-GPU test включає real eBPF parent/child new-open check для Exact та
320 concurrent API writes. Навіть його pass НЕ заміняє 3/15-GPU matrix,
реальну NVIDIA-перевірку, service restart reconciliation або всі три профілі.
Форк на хості досі 6213983, Mode=1, PID 218561; KWin/llama invocation IDs
лишилися `ff4db9d153cd4c159296be85edcaa666` /
`efe4e0b1876c4393ad62c023d1ebbe12`. Немає нового deployment/commit/push.

## Продовження 2026-10-04: виправлена спостережуваність Smart, тільки offline

Після невдалого тесту о 00:57 змінено source diagnostic
`diagnostics/test-cardwire-smart-runtime.py` та його offline tests.
Cardwire `core/inode.rs::sys_drm_inodes` справді додає NVIDIA symlink-и
`/sys/class/drm` до blocked map. Повторне перерахування DRM з ungranted
контролера під Smart не є надійним доказом фізичного зникнення монітора.
Попередній live-тест НЕ перекласифіковано в успішний: проміжні результати
він не зберіг, а actual presentation на всіх виходах не було зафіксовано.

- Новий guard бере **живі** enabled outputs і renderer із KWin D-Bus
  `supportInformation`. Прив'язка назв виходів до NVIDIA береться зі
  unrestricted Hybrid preflight; неоднозначні однакові connector names на
  різних GPU або невідомий формат KWin відхиляються до зміни політики.
- Після rollback до Hybrid повторно перевіряються GPU/card/PCI mapping,
  усі початкові enabled NVIDIA outputs/EDID, actual AMD renderer та identity
  того самого KWin. Перепідключення/заміна монітора під час цього короткого
  діагностичного тесту не вважається успішним незмінним станом.
- Контролер/його дочірні процеси НЕ отримують GPU grant. Немає додаткового
  observer daemon, зміни Cardwire або послаблення root-denial check.
- Атомарні checkpoints `result.json` зберігають кожен access/CUDA/render
  result, stage/failure і rollback outcome. Помилка останнього guard більше
  не стирає попередні результати. Failed rollback не вимикає watchdog;
  post-rollback observation failure не повторює restart.
- Observations Mode=Smart прив'язані до unique D-Bus owner тестового
  Cardwire. Перехід watchdog до Hybrid не може дати хибний pass.
- Report лишає `visible_output_verified=false` та `applied_profile=null`:
  навіть успішний diagnostic не є активацією Work або доказом повної ізоляції.

Offline: **39 Smart + 57 KWin + 49 planner + 7 API tests = 152 passed**;
syntax compilation і diff whitespace check пройшли. Розбір поточного KWin
report read-only дав eDP-1/HDMI-A-1/DP-7 enabled. Smart/hardware запуску нового
коду ще НЕ було. Runtime-артефакти старої спроби залишені на місці: перед
наступним погодженим тестом треба перевірити їх rollback/timer/unit state й
зберегти окремо, не перезаписувати і не повторювати старий controller.

Після роботи: Cardwire PID 218561 / Mode=1; KWin wrapper 187650,
invocation `ff4db9d153cd4c159296be85edcaa666`; llama 5151,
invocation `efe4e0b1876c4393ad62c023d1ebbe12`, усі незмінні. eGPU timers
відсутні. Високе навантаження RTX від KWin лишається окремою невирішеною
перевіркою; не називати цю правку performance fix або готовими профілями.

## Продовження 2026-10-04: exact-process policy та новий дозвіл на AMD trial

У `cardwire-stable-process-access` підготовлено незакомічений opt-in
`Allow_dGPU_Exact` (value=1, status=AllowedExact): grant конкретному TGID у
Smart без успадкування через parent-PID lookup. Shared policy нормалізує
власний Exact до effective Allowed, але ігнорує Exact батька. Legacy Allow/
Force/Manual semantics не змінено; root-authentication збережено. Жодного
env hint для Exact, blanket unit/root allow або deployment не додано.

Це лише частина вузьких display/driver exceptions: успадковані/передані FD,
старі відкриті пристрої, app/env/built-in дозволи, exec/restart lifecycle й
PID reuse не розв'язані цим патчем. Планувальник та Smart live diagnostic
досі використовують старий API; не видавати нове джерело за installed feature.

Збірка у вже наявному build container `cardwire-pr-validation`, source copy
`/build/exact-policy.ZGkn6H`; Rust 1.95.0 + eBPF nightly-2026-08-12.
`cargo test --offline --locked -p cardwire-policy -p cardwire-daemon`:
**94 daemon + 7 shared-policy tests passed**, embedded eBPF зібрався.
Rustfmt pinned toolchain пройшов; VM probe syntax пройшов.
`cargo clippy --offline --locked -p cardwire-policy -p cardwire-daemon
--all-targets -- -D warnings` також завершився успішно (Rust 1.95.0).
Розширений two-GPU VM probe включає parent/child new-open порівняння й
320 concurrent writes, але **VM не запускали** (у build container немає
`/dev/kvm`, Nix store порожній). Перед deployment потрібен повний CI/VM gate
і новий дозвіл на restart Cardwire. Бінарник на ПК лишився 6213983/Hybrid.

Користувач відповів **«Так, усе збережено»** на новий запит дозволу AMD-render
trial з 25-хвилинним watchdog. Root preflight (exec 33982) пройшов; запуск
через exec 57339 перервав зв'язок із desktop. **Не повторювати цей запуск**:
handle вже missing, але systemd journal підтвердив успіх controller.

- 00:48:56–00:49:03: контрольований перехід, одна reconciliation-зупинка
  залишеної graphical target; login manager відкрито один раз, exit success.
- 00:52: actual KWin renderer AMD Radeon 890M (radeonsi). NVIDIA card0-DP-7
  і card0-HDMI-A-1 connected+enabled; AMD eDP-1 теж enabled.
- KWin wrapper 187650 / invocation `ff4db9d153cd4c159296be85edcaa666`.
  Llama незмінна 5151 / `efe4e0b1876c4393ad62c023d1ebbe12`.
  Cardwire незмінний 5412 / `b8d34da2c458499198ba499305dbc147`, Hybrid.
- Під час переходу були NVIDIA Flip event timeout (00:49:00 і :18), старий
  KWin 6730 писав atomic commit/output errors о 00:49:00–01. Це не причина
  оголошувати нинішню сесію зламаною або журнал повністю чистим.
- На питання про обидва монітори/переміщення/прокручування користувач
  відповів **«Так, зображення і робота нормальні»**. Після цього root
  `--confirm --visible-output-ok` (exec 80607) пройшов. Timer вимкнений,
  authoritative state inactive/no jobs. До підтвердження list-timers
  показував 01:15:19 EEST; зараз **автоматичного logout немає**.
- Fresh child-only AMD env: GLX і Wayland EGL обидва показали hardware AMD,
  exit 0 без попередніх DRI3 errors. Це ще **Hybrid**, не Smart isolation;
  не приписувати цьому економію VRAM чи routing всіх звичайних програм.

AMD-first тимчасовий override зберігається до manual restore/reboot. Для
відкату потрібен окремо погоджений restart сесії; після нього підтвердження
baseline через `--confirm-restored --visible-output-ok`, не лише SDDM active.
Користувач окремо погодив короткий Smart diagnostic на встановленому
6213983: **«Так, запускай короткий тест»**. Запуск `--apply-amd-render`
(exec 59768) завершився exit 1: фінальна перевірка повідомила
`The AMD-primary presentation trial requires an enabled NVIDIA monitor`.
Скрипт підтвердив відновлення початкової політики; о 00:58 Cardwire вже
Hybrid (Mode=1), PID 218561, rollback timer inactive/no jobs. KWin і llama
зберегли наведені вище PID/invocation; графічна сесія не перезапускалася.
Користувач підтвердив **«куб був»**. Це visual evidence запуску куба,
але НЕ успіх усіх перевірок: `result.json` записується лише після фінального
guard, тому exception втрачає зібрані probe results. Не називати GLX/EGL/CUDA
та ізоляцію перевіреними за цією спробою. Можлива втрата видимості DRM для
самого контролера під Smart; не відрізнено від фактичної зміни стану виходів.
Після rollback обидва NVIDIA-виходи connected+enabled, actual KWin renderer
AMD Radeon 890M. Новий Smart-тест без погодження не запускати.

На скріні користувача NVIDIA GPU=100%, memory=61%, AMD GPU~50%, memory=97%.
Read-only діагностика після тесту:

- AMD VRAM 520409088/536870912 bytes (~496/512 MiB); GTT
  3947872256/12111990784 bytes (~3.68/11.28 GiB). Індикатор 97% не означає
  заповнення всієї доступної AMD системної пам'яті.
- NVIDIA ~9895 MiB used: llama worker 17712 займає 9556 MiB, KWin 187661
  лише 13 MiB. Пам'ять не доводить джерело GPU utilization.
- `nvidia-smi pmon` у кількох коротких вибірках показав KWin SM 24–76%,
  llama SM `-` (недоступно, НЕ нуль). GPU query також показав 65%, ~65W.
  Отже, NVIDIA виконує помітну роботу KWin навіть при AMD renderer; точне
  джерело початкових 100% та роль cross-GPU presentation ще не встановлені.
- Kernel journal від 00:56 за фільтром NVRM/Xid/Flip timeout/DRM/AMDGPU
  errors не містив записів. Це не скасовує попередніх transition errors.

Під час цієї діагностики політики, дисплеї та inference не змінювалися;
нових навантажувальних тестів не запускали. Work-профіль ще не готовий.

## Поточне: після втрати GUI виправлено trial controller, тільки offline, 2026-10-03

Цей розділ замінює опис актуального стану в історії нижче. **Новий logout,
перемикання GPU чи live-test у цьому проході не виконувалися.** Користувач
дозволив виправити код після діагностики; окреме погодження збереженої роботи
потрібне перед наступним disruptive test. Не змінювати power button/sleep,
Cardwire policy, kernel/PCI/USB4 чи inference заради цього виправлення.

П'ята спроба та аварія відкату (попередній boot
`5017de18da894fc9bb39181023cb42af`):

- 23:06:47–23:07:15: trial, SDDM, новий login; о 23:10:55 actual KWin
  renderer AMD Radeon 890M. Користувач попросив більше часу, але обіцяне
  подовження таймера **не було застосоване**.
- 23:12:42: 5-minute watchdog почав rollback; 23:12:44 зупинив SDDM.
- 23:12:59: controller вичерпав 15-секундний observation deadline, але
  `max(0.01, remaining)` запустив ще один `systemctl show` з 10ms timeout.
  Exception обірвав відкат, SDDM залишився зупиненим.
- 23:13:02–10: systemd сам завершував завислий KWin через stop timeout/
  SIGABRT; старий cgroup зрештою завершився, але controller вже вийшов.
- Після явного дозволу recovery запущено вручну; 23:19:38 SDDM активний,
  23:20:27 новий KWin. Це **не було успішним підтвердженням GUI**:
  `Flip event timeout`, потім багато `atomic commit failed` (EINVAL/EACCES)
  та `Applying output configuration failed!`. Користувач був змушений
  reboot. Точну причину NVIDIA/DRM failure не встановлено.
- Новий boot `1b30b279eb314614967d98a1160f316d` з 23:26:24: actual NVIDIA
  KWin renderer, `nvidia-smi -L` успішний, SDDM/KWin running, Cardwire Hybrid.
  Llama вже нова після reboot (PID 5151 / invocation
  `efe4e0b1876c4393ad62c023d1ebbe12`), не повторювати старе «PID незмінний».
  Runtime trial/watchdog service/timer not-found/inactive; pending rollback
  не було. `/run` artifacts минулої спроби зникли при reboot.

Зміни лише в `diagnostics/test-amd-primary-kwin.py` і його offline tests:

- Observation deadline перевіряється **до** кожного subprocess; жодних
  штучних 10ms queries після закінчення часу. Повільні read-only queries
  повторюються тільки в межах бюджету, без додаткового stop.
- Forward teardown: 30s на gate SDDM і 30s на user-session teardown;
  recovery: окремі бюджети до 120s кожний. Це не зміна systemd unit
  TimeoutStopSec (на хості 15s), не kill/reset. Повністю завислий DRM може
  й далі потребувати ручного відновлення; автоматичного гарантування GUI нема.
- SDDM stop nonblocking, результат перевіряється за станом. Незмінні
  запобіжники: no old KWin PIDs/cgroup, та сама invocation під час teardown,
  завершені targets, порожня user job queue перед рівно одним start SDDM.
  One-shot reconciliation residual graphical target лишається; жодних
  terminate-user, kill inference, скасування сторонніх jobs або reset GPU.
- `recovery-required.json` зберігає причину невдалого відновлення. Watchdog
  не повторює невдалий rollback автоматично. Після успішного start SDDM
  механічний результат фіксується до подальших діагностик, щоб їх помилка
  не спричиняла ще один logout нової сесії.
- `restored` означає лише механічний rollback конфігурації. Новий
  `restore-result.json` явно має `visible_output_verified=false` до
  підтвердження людиною; це не можна називати «GUI відновлено».
- `--confirm-restored --visible-output-ok`: після реального спостереження
  користувача перевіряє новий active KWin, actual renderer, який збігається
  з NVIDIA baseline preflight, enabled NV scanout та незмінну inference.
  Не перезапускає нічого. Archive started trial вимагає `restore-confirmed`.
- Trial watchdog default **1500s (25 хвилин)** від запуску controller,
  збільшено з 15 хвилин на явне прохання користувача. На `--start`
  можна задати `--timeout-seconds 1800` (межі 300–3600). Час збережено у
  state і саме його використовує timer; прапорець **не подовжує** вже
  запущений trial. `--confirm` теж вимагає `--visible-output-ok`.

Offline regression cases включають actual execute→timeout→restore path із
45-секундним завершенням KWin, permanent stuck state, повільний SDDM,
deadline-expired query, відсутність повторного logout та visual-result gate.
Фінальна перевірка: **135 offline tests пройшли** (57 KWin controller,
49 desktop planner, 22 Smart runtime guards, 7 Cardwire API probe).
Наприкінці KWin PID 6698 / invocation `9769b389742344bca16619cd8efee755`
та llama PID 5151 / invocation `efe4e0b1876c4393ad62c023d1ebbe12` незмінні;
trial/watchdog units not-found/inactive, без jobs.
Live controller ще **не випробувано** після цих змін. Без окремого дозволу
не staging/start trial; перед ним read-only preflight актуального source.

## Четвертий trial: AMD renderer запустився, потім timed rollback, 2026-10-03

Після нового явного дозволу «Да давай» виконано root preflight, архівацію
завершеної третьої спроби й staging актуального source. Exec handle 69310
після logout відсутній; повторно цей запит не запускати. Systemd-controller
завершився успішно, а не завис на авторизації.

- 22:26:29: незалежна служба стартувала, watchdog поставлено на 5 хвилин.
- 22:26:34: єдина reconciliation-зупинка залишеної active graphical target.
- 22:26:35: targets inactive/no jobs, старий KWin/cgroup завершені; SDDM
  відкрито один раз із новим порядком GPU. Controller exited successfully.
- 22:28:57: фактичний KWin renderer **AMD Radeon 890M Graphics (radeonsi)**,
  Environment `KWIN_DRM_DEVICES=/dev/dri/card1:/dev/dri/card0`.
- DRM: NVIDIA `card0-DP-7` та `card0-HDMI-A-1` connected+enabled; AMD eDP-1
  теж enabled. Це стан DRM, не візуальне підтвердження користувача.
- Новий KWin wrapper PID 1588229, InvocationID
  `f35af3a128f646abbd3498e56d21fdf0`. Llama PID 777146 та її InvocationID
  незмінні; Cardwire лишився Hybrid, його не перезапускали.

**Visual confirmation не надійшло; watchdog штатно відкотів trial.**
Прогноз за wall clock 22:31:29 не був точним: фактичний systemd list-timers
показав 22:32:16, rollback почався 22:32:18, завершився 22:32:24.
На rollback знову знадобилася рівно одна reconciliation-зупинка; після неї
login screen відкрито один раз, `RESTORED` та успішний exit є в журналі.

О 22:58 read-only перевірка: trial/watchdog service/timer not-found, inactive,
без PID/jobs; runtime override відсутній. KWin знову **NVIDIA**, wrapper PID
1620938, InvocationID `038f92c81c124d52beedf2cc9a562150`; llama незмінна
(777146 / `acd720801ac6443da31e43694537052a`), Cardwire Hybrid.
Тобто активних/queued переходів більше немає. Не запускати повторно exec
handle або staged trial і не обходити restored marker.

Додаткова перевірка журналу: `nvidia-drm Flip event timeout on head 0`
о 22:26:51, ще до повідомлення нового KWin про DRM backend о 22:26:53.
Такий самий timeout є також у NVIDIA baseline/інших переходах цього boot
(10:06, 17:55, 18:39, 22:00, 22:32 та пізніше). Не приписувати його
AMD renderer без окремого доказу й не стверджувати «усі graphics logs чисті».
NVIDIA 615.71.09, kernel 7.2.7-ogc1.1.fc44.x86_64. У перевіреному інтервалі
немає NVRM Xid; це не замінює перевірку видимого зображення.

Технічно forward transition, AMD compositor і rollback тепер перевірені
наживо. Користувач згодом відповів: **«не помітив чесно кажучи ніяких
аномалій. ще раз?»** Це позитивне ретроспективне спостереження зображення,
не формальний тест усіх app backends або Smart isolation. Попередня сесія
вже відкотилася, тож --confirm для неї не виконувати.
Користувач погодив повторний AMD-primary trial; перед новим staging знову
перевірити baseline й архівувати лише завершений runtime. Новий logout не
розширює дозвіл до Smart policy changes чи перезапуску inference.
Smart isolation/VRAM savings/інші два профілі цим іще не перевірені.

## Остання спроба AMD-primary: rollback, 2026-10-03 22:00 EEST

Історія третьої спроби; поточний стан описаний вище.
Після нового дозволу користувача «Ок давай» запущено третій trial із secure
stable Cardwire. Після повідомлення «Перезайшов» перевірено: **AMD-primary
не активувався**; контролер знову відкотів власний override, KWin рендерить
на NVIDIA. Це помилка переходу сесії, не негативний результат AMD scanout.

Факти журналу:

- 22:00:17.712: workspace/workspace-wayland зупинені; SDDM зупинений .756.
- 22:00:24.498: старий KWin завершився.
- 22:00:24.934–25.288: знову стартував `xdg-desktop-portal.service`.
- Перша спроба stop не залишила `graphical-session.target` inactive;
  controller вичерпав 15-секундне очікування.
- 22:00:34.967: другий stop уже під час rollback зупинив graphical target.
- 22:00:35: повернуто baseline SDDM, NVIDIA override trial відсутній.

Installed portal unit містить Requisite/After/PartOf для graphical target.
У systemd 259.9 Requisite-start створює verify-active job, який може замінити
конфліктний stop; активний requisite також утримує StopWhenUnneeded target.
Це узгоджується з повторним стартом portal, але конкретний caller скасування
не зафіксований info-журналом. Не приписувати це доведено Plasma/Flatpak.
Джерела: systemd [dependency atoms](https://raw.githubusercontent.com/systemd/systemd/v259.9/src/core/unit-dependency-atom.c)
та [job handling](https://raw.githubusercontent.com/systemd/systemd/v259.9/src/core/job.c).

Read-only стан після rollback: KWin wrapper PID 1526882, InvocationID
`c6045a1335d24e668c87f6fe46c4e28a`; llama PID **777146**, InvocationID
`acd720801ac6443da31e43694537052a` незмінні. Cardwire secure fork лишається
Hybrid. Trial failed/MainPID=0/Job порожній; watchdog service/timer inactive
і без jobs. Runtime artifacts збережено; не стартувати стару runtime-копію.

Після двох незалежних read-only reviews source контролера виправлено:

- Nonblocking stop із тим самим 15-секундним observation deadline.
- Рівно одна додаткова зупинка лише `graphical-session.target`, коли вона
  active без job, обидва workspace targets вже terminal/no jobs, а старий
  KWin має MainPID=ControlPID=0 та порожню/видалену service cgroup.
- Перевірки SDDM, InvocationID, queued jobs; невідомий стан не приймається.
- Немає скасування сторонніх jobs, mask portal, terminate-user або змін llama.
- Перед поверненням greeter, включно з rollback, потрібна порожня черга
  user-manager jobs. Це консервативно: за невідомої/завислої черги login
  лишається закритим для ручної діагностики, не запускається поверх stop.
- У журнал одразу пишуться помилки stop і зміни станів із Job/RequisiteOf.
- Rollback тепер також вимагає завершеного старого desktop, а не лише
  порожньої черги; після успішного відкриття SDDM чергу повторно не перевіряє,
  щоб jobs нового логіна не перетворювали успішний відкат на новий teardown.
- Пройшли 38 AMD-primary trial, 49 planner, 17 Smart guard і 7 API probe
  offline tests: **111**. Host sessions/services тести не змінюють.

**Наживо цей source ще не запускався. Новий logout не дозволений автоматично.**
Для наступної спроби: актуальне підтвердження збереженої роботи, root read-only
`--check`, `--archive-restored` тільки завершених artifacts і нове staging
через `--start --restart-session`. Тоді перевірити actual AMD renderer і
видиме NVIDIA scanout; лише після цього повертатися до Smart isolation.

### Offline продовження, поки очікуємо дозвіл на наступний logout

Read-only перевірка знову показала terminal trial/watchdog без PID/jobs;
KWin та llama мають ті самі invocation/PID, новий live-test не стартував.
Окремо посилено `test-cardwire-smart-runtime.py`: попередній evaluator
відсікав лише негативні рядки/коди, тож порожній набір або успішний запуск
не на AMD міг виглядати як готовий до visual confirmation.

Тепер потрібен рівно один результат кожної точної команди GLX, Wayland EGL
та Wayland Vulkan; кожна мусить завершитись exit 0 без render errors та
показати фактичний AMD renderer у своєму renderer/Selected GPU полі.
Vendor/env hints, порожній output, неправильна команда, NVIDIA, software
fallback або неповний probe set не проходять. Structured renderer assessment
зберігає причини відмови і завжди лишає `visible_output_verified=false`:
автоматичні probes не підміняють візуальне підтвердження.

Пройшли **116 offline tests**: 38 KWin, 49 planner, 22 Smart guards, 7 PID API.
Зміни лише в repo source/tests, без Cardwire policy writes, перезапусків
служб, нових grants або graphics/session transitions. Наступний live gate
не змінився; автоматичне продовження goal не є дозволом на logout.

## Актуальний baseline після встановлення secure fork, 2026-10-03

Цей розділ замінює старі твердження нижче про тимчасовий binary override,
потребу залишити patched daemon та непрацездатний PID API. Старі результати
збережені як історія тестів, не як опис поточної інсталяції.

Користувач явно погодив установлення й запустив `deploy-secure-6213983.sh`.
Установлено stable fork **0.12.3**, commit
`62139830260839c037c97e6a8dc607cc47559b90`, із root-authenticated PID API та
єдиною атомарною PID-policy map. Гілка `backport/v0.12.3-secure-policy`
у `keefeere/cardwire`; основна гілка форку окремо містить alpha-версію, її на
цей ПК не ставили. CI stable (build, Clippy, VM 2/3/15 GPU) повністю пройшов:
https://github.com/keefeere/cardwire/actions/runs/37141779614.

Постійний override:
`/etc/systemd/system/cardwired.service.d/95-local-secure-policy.conf`.
BindReadOnlyPaths указує на
`/var/opt/cardwire-fork/releases/62139830260839c037c97e6a8dc607cc47559b90/bin/cardwired`.
SHA256 демона:
`799918680da1f5e9c4458357e83d9b76f7d49f722eee52bd615d8f8e62b7bf4a`.
RPM і `/usr/bin/cardwired` поза service namespace не замінені; CLI/GUI пакета
залишилися штатними. Старий `90-egpu-profile-api-test.conf` більше не активний.

У наданому користувачем звіті інсталяції: 21 root API check і 10 відмов
непривілейованому caller пройшли; config/mode hashes і KWin/llama процеси
незмінні, `nvidia-smi -L` успішний. Це **не тест Smart isolation або рендерингу**.
Після цього read-only перевірка підтвердила active cardwired/MainPID 1496107,
саме постійний override, Mode Hybrid. KWin wrapper PID 1078326, фактичний
renderer NVIDIA; llama.service PID 777146 та InvocationID
`acd720801ac6443da31e43694537052a` не змінилися. Ці PID — snapshot, не allowlist.

Відкат інсталяції (лише в Hybrid):

```bash
sudo bash /var/lib/cardwire-fork-deploy/6213983/deploy.sh --rollback --force
```

На тому самому boot відновлює попередній runtime-патч; після reboot — штатний
демон, якщо його пакет не змінився. Не застосовувати старий runtime rollback
до нової постійної інсталяції. Kernel/RPM/сесія в цій установці не змінювалися.

Підготовка продовження:

- `test-cardwire-smart-runtime.py` тепер вимагає SHA secure stable build,
  перевіряє executable унікального D-Bus owner після рестарту та новий daemon
  hash після rollback. Старі runtime-копії не переписані.
- Planner `--probe-process-access` без root повертає `requires-root`, не
  запускає навіть діагностичний child і не трактує очікуваний AccessDenied
  як поломку Cardwire. Звичайний read-only plan залишається доступним.
- Runtime drop-in для private Smart trial тепер створює свій каталог, якщо
  його немає після reboot, та відмовляється перезаписувати наявний файл.
- Пройшли 49 planner, 17 Smart guard, 23 AMD-primary KWin trial і 7 API probe
  offline tests (96). Це не заміна live-тесту AMD compositor.
- Попередній AMD trial справді завершений: service failed/MainPID=0/Job
  порожній; watchdog timer not-found. Новий session transition не запускався.

Наступний gate залишається тим самим: **погоджений вихід із desktop-сесії**
для AMD-primary KWin + NVIDIA scanout; новий порядок із закритим SDDM greeter
до кінця teardown описаний нижче; результат третього trial та нове виправлення
описані в останньому розділі на початку цього файла. Спершу
root `--check` та `--archive-restored`, потім лише з актуальним підтвердженням
готовності `--start --restart-session`. Не повторювати Smart-render probe,
доки користувач не підтвердить видимий AMD-render desktop на NVIDIA-виході.
Профіль Work/iGPU, повна політика вузьких службових винятків, restart workers,
native/Flatpak/Electron presentation, виміри VRAM та повернення Gaming досі
не перевірені; не оголошувати три профілі завершеними.

## Поточний результат live-тестів, 2026-10-03

Після окремого дозволу користувача «так, мож все робити» patched Cardwire
залишено **до reboot**, без заміни RPM. Поточний runtime override:
`/run/systemd/system/cardwired.service.d/90-egpu-profile-api-test.conf`.
Він підміняє лише `/usr/bin/cardwired` усередині unit namespace. Mode знову
Hybrid; SDDM PID 5212 і llama PID 777146 не змінилися протягом цього етапу.
Відкат binary (спочатку має бути Hybrid):

```bash
sudo bash /run/egpu-cardwire-profile-test/rollback.sh --force
```

`diagnostics/test-cardwire-api-runtime.sh --keep-until-reboot` повторив
21 API check перед залишенням патча; звичайний `--apply` досі робить rollback.
Не запускати його вдруге поверх існуючого runtime-каталогу.

Новий `diagnostics/test-cardwire-smart-runtime.py` перевірив Smart із
**приватними копіями** `/etc/cardwire` та `/var/lib/cardwire` у mount namespace
демона. Тому тест не залишає Smart у штатних файлах навіть після reboot.
Watchdog і finally прибирають тільки власний `91-egpu-smart-test.conf`,
перезапускають Cardwire зі штатною політикою та перевіряють hash оригінальних
cardwire.toml/mode.json. Writes адресуються UNIQUE D-Bus owner тестового
демона, щоб запізнілий controller не записав Smart у вже відновлений daemon.

Два тести завершилися й відкотили політику. Їхні артефакти (root-only):
`/run/egpu-cardwire-smart-test`, `/run/egpu-cardwire-smart-render-test`.
Обидва watchdog timers неактивні; BindPaths більше немає, лишився лише binary
BindReadOnlyPaths. ROOT каталоги не видаляли/не перезаписували.

Результат доступу до пристроїв позитивний:

- Новий UID 1000 без дозволу: NV card/render/nvidia0/nvidiactl повертають
  ENOENT; AMD render відкривається.
- Новий явно дозволений UID 1000: обидві GPU доступні, `cuInit` успішний,
  CUDA device count = 1. **Обчислення/llama worker restart цим не перевірено.**
- Новий root-процес без дозволу: NVIDIA також недоступна, AMD доступна.
- 35 вже запущеним GPU-клієнтам тимчасово дали grants, щоб не обірвати
  поточний desktop/inference. Це НЕ цільова Work isolation і НЕ VRAM savings.
  Після rollback/restart демона ці тестові PID maps очищені.
- У першому preflight навіть PID 1 тримав NV FD. Йому НЕ давали grant:
  такий дозвіл поширився б на системні служби. Наявні FD не відкликаються.

**Рендеринг під NVIDIA-primary KWin не пройшов:**

- GLX заявив AMD через Zink, exit 0, але stderr містив failed DRI3 і
  CreateSwapchainKHR/VK_ERROR_INITIALIZATION_FAILED.
- Wayland EGL вибрав `llvmpipe` (software), теж exit 0.
- Wayland vkcube вибрав AMD, але не завершив 120 frames і був завершений
  власним timeout через 12 секунд. Видиме коректне виведення не підтверджене.

У raw result.json поле `status: passed` відносилося лише до device-access
checks (scope це обмежував). Формат звіту вже виправлено: renderer errors,
software fallback або timeout більше не дають загальний зелений результат.
Історичні raw artifacts не змінювали. 48 planner + 9 Smart guard + 7 API
offline tests пройшли.

Наступний кандидат Work/NVIDIA — **AMD-primary KWin із обома DRM nodes**,
тобто AMD rendering + фізичний NVIDIA scanout. Планувальник тепер генерує
AMD:NV для `work-nvidia`, NV:AMD для Gaming і лише AMD для `work-igpu`.
Це НЕ застосована/перевірена конфігурація. GPU, до якої підключено монітор,
не слід ототожнювати з GPU рендерингу композитора.

### Перша спроба AMD-primary: відкотилася ще під час logout

Користувач окремо дозволив вихід із сесії та підтвердив збереження роботи.
`diagnostics/test-amd-primary-kwin.py --start --restart-session` запущено
о 17:53:43. Незалежна systemd-служба поставила 300-секундний watchdog,
тимчасовий per-unit `KWIN_DRM_DEVICES=AMD:NV` і почала завершувати Plasma.
О 17:53:45 запит stop повернув `Job for graphical-session.target canceled`.
Скрипт сприйняв це як провал, прибрав власний override і відновив SDDM.
Журнал user manager показує, що targets усе-таки зупинилися, а старий KWin
завершився о 17:53:47. Це **не тест невдалого AMD рендерингу**: нова
AMD-first сесія не була підтверджена.

О 18:16 після повідомлення користувача про зникнення світла перевірено:
host не reboot-ився (boot почався о 10:04:45), trial service failed/MainPID=0,
watchdog timer відсутній, KWin override відсутній. Новий активний KWin
показує NVIDIA renderer; llama зберегла PID 777146 та InvocationID
`acd720801ac6443da31e43694537052a`. Cardwire досі patched, Mode Hybrid.
Не приписувати скасування job відключенню електрики: причинного доказу немає.

Виправлення в source чекає до 15 секунд на фактичне завершення обох
graphical targets і старого KWin, навіть якщо stop-job скасовано. Сам код
повернення тепер не доводить ані успіх, ані провал; живий/невідомий стан
забороняє продовження. Не зупиняє user manager або llama. 20 offline тестів
KWin trial, 48 planner, 9 Smart guard та 7 API probe пройшли. **Виправлена
спроба наживо ще не запускалася.** Додано `--archive-restored`: перевіряє
restored-marker, відсутність живих/queued trial і watchdog units, відсутність
власного KWin override та незмінність llama; лише тоді переносить усі старі
артефакти у приватний унікальний каталог. Незавершений тест не архівує.
О 18:23 цей підготовчий крок успішно виконався без рестартів:
`/run/egpu-amd-primary-kwin-test-archive-pj0ch16d/trial`. Старий failed transient
unit звільнено; його LoadState тепер not-found. Наступний root `--check`
пройшов: AMD card1 + NVIDIA card0, Hybrid, llama PID 777146 та KWin wrapper
PID 883256 незмінні. Артефакти не видаляли. Не запускати архівовану стару
runtime-копію: повтор дозволений лише новим source `--start --restart-session`
після підтвердження користувачем готовності до нового виходу із сесії.

Користувач згодом відповів «ок готов». Новий source `--start --restart-session`
запущено через pkexec о 18:32, після очікування авторизації служба стартувала
о 18:38:55. Exec handle 53128 тепер відсутній; це **завершена друга спроба**,
не pending authorization. Вона знову відкотилася; користувач повідомив про
два входи. Деталі журналу:

- 18:38:57 — Plasma workspace зупинено, 18:39:03 — старий KWin завершився.
- 18:39:03 — ще працюючий SDDM уже показав greeter.
- Скрипт чекав graphical-session.target, який залишався active; після
  15-секундного граничного очікування почав rollback.
- 18:39:11 — користувач почав login; 18:39:13 — rollback перезапускає SDDM.
  Це перервало перший login. Новий SDDM стартував о 18:39:23, успішний
  повторний login зафіксовано о 18:39:33.
- Trial failed, override прибраний. Фактичний renderer знову NVIDIA.
  Llama PID/InvocationID збереглися; новий KWin wrapper PID 1078326.
  Watchdog згодом завершився без додаткового перезапуску: restored-marker
  робить його no-op. Timer/service тепер not-found.

Root cause подвійного login — екран входу залишався доступним під час
незавершеного переходу, а потім rollback перервав уже розпочатий вхід.
Не робити висновку про працездатність AMD-render з цього результату.
Source виправлено: спершу `stop sddm`, перевірка його inactive/MainPID=0,
потім завершення session targets/compositor, і лише після цього `start sddm`.
На помилці trial не відкриває login до зовнішнього rollback; rollback
спочатку прибирає свій override і завжди намагається повернути SDDM через
ідемпотентний `start`, навіть якщо teardown не вдався. User manager/llama
не зупиняються. 23 offline KWin tests пройшли, включно з login-race та
failure-recovery guards. **Цей порядок наживо ще не перевірено; третій
вихід із сесії не запускався.** Runtime-каталог другої спроби збережено;
для повтору спершу `--archive-restored`, потім нове погоджене `--start`.

Для наступного етапу підготовлено `test-cardwire-smart-runtime.py --apply-amd-render`:
окремі артефакти `/run/egpu-cardwire-smart-amd-render-test`, власний watchdog,
перевірка справжнього AMD hardware renderer KWin і ввімкненого NV monitor
перед зміною Cardwire та після probes. Старі NV-primary результати не затирає.
11 offline Smart tests пройшли; цей новий режим **наживо ще не запускався**.
Спочатку підтвердити видимий AMD-primary/NVIDIA-scanout desktop.

Для наступного тесту потрібен попереджений logout/login з врятованою роботою;
не перезапускати SDDM непомітно. `Linger=yes`, llama.service WantedBy=default.target,
PartOf порожній; не зупиняти його та не міняти модель/KV/контекст.
Після зміни compositor primary повторити hardware rendering/visible-window
тести, і лише потім вмикати повну політику й міряти VRAM.

Перевірений upstream KWin v6.7.5 wrapper запускає `kwin_wayland` і експортує
в user manager лише DISPLAY/WAYLAND_DISPLAY/XAUTHORITY, не всі environment
variables: https://github.com/KDE/kwin/blob/v6.7.5/src/helpers/wayland_wrapper/kwin_wrapper.cpp.
Точковий per-unit environment для display stack можна досліджувати, але
спочатку врахувати KWin children (зокрема plasma-keyboard-custom) та перевірити,
що CARDWIRE_ALLOW не потрапляє у весь user manager. Читати MainPID як «KWin»
без перевірки executable досі неправильно.

## Cardwire: виправлення API перевірено наживо, 2026-10-03

Користувач окремо погодив форк/патч/PR, збірку та тимчасовий A/B-тест із
перезапуском лише `cardwired.service`. Це уточнює заборону форка нижче:
дозволено вузьке виправлення API, не новий GPU-менеджер і не переписування Cardwire.

- Форк: `keefeere/cardwire`, локально `_repos/_home/cardwire`.
- Upstream PR: https://github.com/OpenGamingCollective/cardwire/pull/279,
  commit `e7e29bd`; 82 daemon unit tests, Clippy та rustfmt пройшли.
- Stable backport: `_repos/_home/cardwire-stable-process-access`, гілка
  `backport/v0.12.3-process-access`, commit `14add40` поверх tag `v0.12.3`.
  Змінено тільки `interface/smart.rs`, не dependencies/eBPF sources.
  95 daemon unit tests, Clippy та rustfmt пройшли.
- Release artifact: `cardwire-stable-process-access/target/local-api-test/cardwired`;
  SHA256 `07baf8f89af7575fedf630c1c741c44b114477ed4a75b9712d7e7e233356d6ff`.
  Поруч `build-info.json` з походженням та командами збірки. Це НЕ RPM.

Фактичний A/B/A на ПК: штатний 0.12.3 — `bpf_map_delete_elem failed`;
patched 0.12.3 — 21/21 API checks passed; повернений штатний — знову та сама
помилка. Перевірено fresh/repeated/switching Allow_dGPU/Force_dGPU/Force_GPU
та незмінний Default на власних тимчасових sleep-процесах. Live patched probe
запущено root-контролером; це не тест видимого рендерингу/ізоляції/VRAM.

Тестове bind-перекриття `/usr/bin/cardwired` було лише у mount namespace
його unit. Оригінальний файл/RPM не змінено. Override видалено, watchdog timer
неактивний, `cardwired` знову штатний, Hybrid збережено. SDDM PID 5212 та
llama PID 498416 не змінилися. NVIDIA після тесту доступна. Тимчасові
root-owned артефакти й журнал лишилися у `/run/egpu-cardwire-api-test` до reboot.

Інструменти: `diagnostics/egpu-cardwire-api-probe.py` (лише власні child PIDs),
`diagnostics/test-cardwire-api-runtime.sh` (check за замовчуванням, apply лише
явно/root, завжди rollback), `tests/test-cardwire-api-probe.py` (7 offline tests).
Контролер відмовляє повторному запуску, якщо попередній runtime-каталог існує:
не стирати/перезаписувати його без перевірки стану.

Постійної інсталяції патча та активації Work ще НЕ було. Встановлена версія
залишається `cardwire-0.12.3-2.fc44.x86_64` з API defect. Потрібен окремий дозвіл
на залишення patched daemon активним і на зміну Mode/перезапуск сесії.
На хості вже є стороннє staged deployment rpm-ostree: не чіпати його.

## Аудит вузьких службових винятків, 2026-10-03

Планувальник тепер має опційний read-only `--audit-service-scopes`:

```bash
python3 ./egpu-desktop-profile-plan.py work-nvidia --audit-service-scopes
```

Він читає точні system/user units, їхні cgroup members (включно з worker
підгрупами), PID/PPID/start time/UID/executable та повторно перевіряє стан
unit. Не читає argv/environment, не дає grants, не перезапускає служби.
Список PID — лише snapshot, не готовий allowlist для наступного запуску.
48 offline tests планувальника пройшли, окремі 7 API-probe tests теж.

На цьому ПК MainPID `plasma-kwin_wayland.service` — `kwin_wayland_wrapper`,
а не композитор. У тій самій cgroup є KWin, Xwayland і два
`plasma-keyboard-custom`; один із keyboard-процесів — прямий child KWin.
Тому не можна blanket-allow усю unit або автоматично брати її MainPID.
У Cardwire v0.12.3 Smart перевірка Allowed(PID або PPID) випереджає Forced:
дозвіл батькові пропускає його безпосередніх дітей навіть поза цією unit,
і child Force_GPU=0 не скасовує цей дозвіл. У звіті це явно позначено.

Живий unprivileged audit дав `incomplete`: `/proc/PID/exe` недоступний для
cardwired, logind, persistenced і самого KWin (не лише для чужих UID).
Це обмеження читання, НЕ поломка GPU. Ідентичність не вгадується за comm;
повний audit потребує привілейованого read-only запуску. При root-запуску
user-unit запитується через bus налаштованого desktop user, не root manager.
Цю root-гілку поки перевірено тільки offline.

Після аудиту Mode лишився Hybrid, штатний cardwired активний без тестового
BindReadOnlyPaths. Work не активували, llama/SDDM не перезапускали. Запит
дозволу залишити patched daemon до reboot досі не має окремої відповіді;
попереднє «так» стосувалося вже завершеного короткого A/B з rollback.

## Уточнення користувача й початок реалізації, 2026-10-03

Цей розділ уточнює початковий двопрофільний план нижче. Користувач погодив
три профілі: `gaming-nvidia`, `work-nvidia`, `work-igpu`. Дисплеї не фіксовані:
їх можна переносити на нативні виходи HP, але фактичну GPU виведення треба
перевіряти через DRM. DisplayLink не можна автоматично вважати AMD-виходом.
Якщо HP підключений downstream від TH5P4, від'єднання кабелю TH5P4 від хоста
відріже також HP — логічний detach GPU та фізичний unplug усього ланцюжка різні.

Користувач погодив мінімальні явно перелічені службові винятки замість
буквального блокування всіх демонів: Cardwire/вбудовані винятки udev,
обслуговування драйвера, необхідний display stack лише для RTX-виходів,
окремо дозволений inference. Це НЕ загальний дозвіл root або всій Plasma.
Програмування у репозиторії погоджено; install, зміна режиму Cardwire,
logout, перезапуск критичних служб, reboot і suspend не виконувалися й
досі потребують окремого підтвердження. Незалежні dirty-зміни sleep/PM зберегти.

Додано `egpu-desktop-profile-plan.py` — поки що лише інспектор/планувальник,
не перемикач і не інсталлер. Він визначає PCI/DRM за `hardware.conf`, перевіряє
топологію дисплеїв і Cardwire, описує per-unit винятки та candidate environment.
Поле `applied` завжди null: план і renderer query не доводять застосування.
Ні звичайний інсталлер, ні widget цей експеримент поки не викликають.

```bash
python3 ./egpu-desktop-profile-plan.py gaming-nvidia
python3 ./egpu-desktop-profile-plan.py work-nvidia
python3 ./egpu-desktop-profile-plan.py work-igpu
python3 ./tests/test-desktop-profiles.py
```

Exit 2 означає blocker плану, не помилку eGPU. Успішний exit 0 також не
означає, що профіль можна без перевірок активувати. Не копіювати candidate
environment у `/etc` вручну.

Поточний blocker підтверджений на встановленому Cardwire 0.12.3-2.fc44:
`SmartPolicy.RequestProcessAccess(PID, "Allow_dGPU", 1)` для нового власного
діагностичного `sleep` повертає `bpf_map_delete_elem failed`; readback не
показує Allowed. У source tag v0.12.3 (`92c3e96cddf9a062aba36d316aab73eefbf871e2`)
`interface/smart.rs` намагається видалити PID із протилежної BPF map і
передає помилку назовні, навіть коли такого ключа там ще немає, тому до
вставки allow-запису не доходить. Аналогічну реалізацію в main перевірено
2026-10-03. Це не помилка NVIDIA і не доказ непрацездатності всього Smart:
нові процеси з `CARDWIRE_ALLOW` використовують інший шлях через analyzer.

Опційний `--probe-process-access` тестує лише grant тимчасовому дочірньому
процесу, потім завершує й reap-ить його. Він не змінює Mode, Config або
app policy DB. Звичайні команди вище такого тесту не запускають.

У поточному Hybrid `cardwire launch --gpu 0 glxinfo -B` усе ще показав NVIDIA.
Явні Mesa/AMD env дали апаратний AMD renderer, але також DRI3 pixmap errors:
видиме виведення вікон цим НЕ перевірене. Режим Smart наживо не вмикався.
Перевірити root/display exceptions, успадкування SDDM/KWin/Xwayland, worker
exec/restart, restart Cardwire, відкат та реальні native/Flatpak/Electron вікна
перш ніж під'єднувати activation до UI. Не розширювати allow до всієї сесії
і не писати BPF maps/SQLite напряму як workaround цього API defect.

## Мета й межі

Розширити наявний helper двома профілями використання GPU. Не замінювати його Cardwire, не переписувати працездатне підключення eGPU й не створювати ще один GPU-менеджер.

- **Gaming / Ігри:** зберегти поточну перевірену NVIDIA-first поведінку та ігрові можливості.
- **Work / Робота:** звичайні desktop-застосунки переважно на AMD iGPU; RTX доступна вибраним обчислювальним процесам через політику Cardwire. Мета — звільнити частину VRAM для локального inference, зберігши придатний desktop і зовнішній монітор.

Користувач приймає помірне погіршення плавності звичайних desktop-застосунків заради вільної VRAM. Це не означає згоду на чорний екран, нестабільність, втрату сесії без попередження або непомітне перемикання на software rendering.

Зараз користувач продовжує окремо тестувати параметри llama, потім перезавантажить ПК та повторно виміряє VRAM. До цього профілі не впроваджувати. Майбутню роботу почати з read-only аудиту, мінімального плану змін і способу відкату; активація профілю, рестарт сесії, reboot і suspend — лише після окремого підтвердження.

## Відомий контекст — перед роботою перевірити заново

Машина: ASUS ROG Xbox Ally X (XAX), AMD iGPU, Bazzite/KDE Plasma/Wayland. eGPU: RTX 5070 Ti 16 ГБ у TH5P4/JHL9480; у профілі репозиторію також є optional HP Thunderbolt Dock G4. Зовнішній монітор використовується через RTX; зняти фактичну схему активних виходів, роздільностей, частот, HDR/VRR, а не вгадувати її.

README helper описує NVIDIA-first KWin, guarded PCI/BAR staging, ordering NVIDIA/Cardwire/display-manager і safe-detach. Це опис репозиторію, не підтвердження, що встановлена копія ідентична. Перед змінами порівняти локальну інсталяцію та поточну гілку, зберегти користувацькі зміни. На момент підготовки handoff SHA blob README: `5087c923db69b3d37830eeb9ba26634ac527f752` — це НЕ commit всього репозиторію.

Зі слів/логів користувача до майбутнього reboot:
- GPU загалом: 14053 MiB; процес llama: 9498 MiB; різниця — приблизно 4,45 GiB. Це історичний snapshot, не цільовий норматив.
- Decode приблизно 4–5 tok/s, відтворюється також у прямому WebUI.
- Не встановлено, що саме нестача VRAM або CPU offload є причиною низької швидкості.
- Контекст llama був 40960, parallel=1, auto GPU offload; KV F16. Q8 KV обговорюється/тестується окремо, не вважати його вже застосованим.
- У user-unit наведено `ExecStart=%h/.local/bin/llama serve --host 0.0.0.0 --port 9931 --ctx-size 40960 --parallel 1`. У router можуть бути дочірні процеси моделей.

Не змінювати quant, context, KV, версію llama чи модель у межах тесту GPU-профілів. Після поточних експериментів користувача зафіксувати новий baseline.

## Перший етап: read-only аудит

Перевірити версії ядра, Bazzite, KWin, Mesa/NVIDIA і Cardwire; стан служб; встановлений hardware.conf; наявні environment overrides GPU; який GPU рендерить KWin і який фізично виводить кожний дисплей. Не використовувати нестабільні `card0/card1` або GPU-номери без зіставлення з PCI-адресою та перевіреним профілем.

З'ясувати, як Cardwire класифікує XAX, які режими реально доступні та яку GPU вважає default. Документація обмежує Integrated/Smart типом Laptop, а для Desktop описує Hybrid/Manual. Не обіцяти Smart лише за назвою пристрою. Уточнити можливості саме встановленої версії через help, документацію та D-Bus introspection. Не вигадувати API і не редагувати внутрішню SQLite-базу Cardwire напряму.

Перевірити взаємодію з external-display-auto-switch, battery auto-switch, Switcheroo shim та вже наявним керуванням cardwired з helper. Автоматичний перехід у Hybrid при активному RTX-моніторі може скасувати задумане обмеження застосунків. Не вимикати його без перевірки, що зовнішній дисплей залишиться працездатним.

## Контракт профілів

### Gaming

Поточна працездатна конфігурація — baseline і профіль за замовчуванням при міграції. Не змінювати NVIDIA-first порядок, високочастотні режими, HDR/VRR, Steam/Gamescope, guarded Gen3/Gen4, safe-detach та rollback без окремого обґрунтування.

Не зупиняти активний inference автоматично при виборі Gaming. Якщо потрібне звільнення VRAM, показати це користувачу й запросити окреме підтвердження завершення/вивантаження.

### Work

Робоча ціль: рендеринг звичайних програм та, якщо перевірка дозволить, композиція на AMD; політика доступу до RTX — через Cardwire, а не через другий конкурентний менеджер.

Не трактувати Work як «вимкнути NVIDIA». CUDA для дозволених задач має працювати. Перевірити доступ до потрібних NVIDIA/CUDA device nodes для `llama serve`, дочірніх model workers і повторного запуску через systemd. Не дозволяти всю користувацьку сесію лише заради одного сервісу; не обмежуватися PID, який зміниться після рестарту. Спосіб запуску/успадкування дозволів перевірити на встановленому Cardwire.

Перевірити звичайні native/Flatpak/Electron-застосунки: браузер, месенджери, редактор, термінал. Вони мають використовувати AMD-апаратне прискорення, а не CPU software renderer.

**Зовнішній дисплей — жорстка вимога.** Якщо монітор під'єднаний до RTX, композитор повинен зберегти потрібний доступ до NVIDIA DRM/scanout. Не приховувати RTX від KWin так, щоб зник її вихід, і не примушувати композитор бачити лише AMD. Нульова VRAM на RTX не є критерієм успіху: display/driver buffers можуть залишатися.

Якщо повний AMD-first compositor непридатний для цієї топології, спочатку повідомити обмеження і запропонувати мінімальний компроміс усередині Work: KWin/scanout на RTX, звичайні застосунки на AMD. Не плодити додаткові публічні режими без потреби. Не змінювати частоту, HDR або VRR потайки; зниження частоти дисплея — лише окремий погоджений експеримент.

## Мінімальна реалізація й безпечне перемикання

Використати наявний widget/CLI/helper і підтримуваний інтерфейс Cardwire. Не створювати новий daemon, форк Cardwire або універсальний фреймворк профілів. Якщо потрібних можливостей у встановленій версії немає — зупинитися на описі обмеження, а не обходити його ризиковими kernel/device хаками.

Розділити вибраний профіль і фактично застосований стан. UI має показувати «Ігри», «Робота», «очікує перезапуску сесії» або причину відмови. Повторне застосування того самого профілю повинно бути ідемпотентним.

Блокування Cardwire не переселяє вже запущені застосунки на інший GPU. Заздалегідь повідомляти, що треба перезапустити програми або сесію. Для зміни GPU композитора допустимий контрольований logout/login чи reboot, але тільки після підтвердження. Не перезапускати display-manager одразу після натискання без попередження про незбережені дані.

Зберегти попередню робочу політику та лише власні зміни конфігів; не затирати сторонні правила. Узгодити перемикання з існуючим блокуванням detach/reattach/boot-переходів, не виконувати їх паралельно. Підготувати простий відкат через TTY/SSH до зафіксованого попереднього працездатного стану. Повернення на AMD при відсутній RTX — допустиме; не обіцяти зберегти RTX-монітор, коли карта фізично відсутня.

Не змінювати PCI bus numbering, bridge windows, ReBAR, Thunderbolt authorization, kernel arguments або поведінку hotplug заради цього UI-профілю. Не викликати GPU reset/unbind/module unload на активній display/compute GPU.

## Перевірки і критерії результату

Порівняти Gaming і Work з однаковими моніторами, налаштуваннями дисплея та набором заново запущених програм. Окремо заміряти baseline без моделі та з тією самою завантаженою моделлю. Зафіксувати:

- загальну і по-процесну VRAM, CPU/RAM, GPU power; який GPU використовує кожна тестова програма;
- фактичні buffers/offload моделі, TTFT, prefill і decode, а також довжину prompt, prefix-cache/warm-up стан і відсутність паралельних запитів;
- плавність прокручування, відео, переміщення вікон і придатність зовнішнього дисплея, у тому числі під час inference;
- повернення Work → Gaming, звичайний restart сервісів, завантаження без eGPU та регресії вже підтримуваного safe-detach.

Не обіцяти звільнення всіх 4–5 GiB або конкретне зростання tok/s. Успіх Work: вимірюване зниження стороннього використання RTX, працездатний CUDA endpoint, нормальний desktop, передбачуваний відкат і відсутність регресій Gaming. Якщо економія мізерна або cross-GPU copies шкодять більше — показати результат, а не розширювати проєкт.

## Сон — окремий експеримент, не гарантований ефект

Не змішувати runtime D3/D3cold пристрою із system suspend/resume. Робочий профіль може зменшити кількість графічних клієнтів RTX, але це не доводить, що виправляться NVIDIA/CUDA/USB4 suspend-проблеми.

Після стабілізації профілів, з окремим підтвердженням, тестувати спочатку сон із вивантаженою моделлю, потім — із завантаженою без активного запиту. Стан активної генерації — тільки окремий погоджений тест, без втрати роботи. Після resume перевірити дисплей, PCI/BAR/топологію, CUDA та API. NVIDIA power-management параметри не змінювати навмання і не копіювати з документації іншої версії драйвера.

## Очікуваний результат від Codex

Спочатку короткий аудит, підтверджені можливості Cardwire на цій машині, мінімальний diff-план та rollback. Далі — лише погоджений невеликий patch у наявному helper, перевірки доступних сценаріїв і звіт з виміряним ефектом. Не видавати read-only перевірку документації за тестування на залізі. Не виконувати install/reboot/logout/suspend і не публікувати реліз без окремого дозволу.

## Джерела для перевірки перед реалізацією

README helper (прочитаний через підключений GitHub; самі скрипти й локальну інсталяцію в цьому handoff не аудовано):
`https://github.com/keefeere/bazzite-th5p4-nvidia-egpu/blob/main/README.md`

Cardwire: режими і обмеження за типом системи:
`https://opengamingcollective.github.io/cardwire/`

Cardwire: наявні процеси, launch, перемикання при зовнішньому дисплеї:
`https://opengamingcollective.github.io/cardwire/getting-started/usage.html`

Cardwire: точки інтеграції per-app policy; перевірити версію до застосування:
`https://opengamingcollective.github.io/cardwire/development/smart.html`

Bazzite: Cardwire, несумісність паралельного використання інших GPU-менеджерів:
`https://docs.bazzite.gg/Advanced/cardwire/`

Розбір cross-GPU copies від розробника KWin, 2026-07-31; це не benchmark нашої машини:
`https://zamundaaa.github.io/wayland/2026/07/31/fixing-multi-gpu.html`

NVIDIA: загальні механізми system suspend, CUDA та збереження VRAM. Наведена довідка старішої гілки, не інструкція налаштування встановленого драйвера:
`https://download.nvidia.com/XFree86/Linux-x86_64/570.133.07/README/powermanagement.html`
