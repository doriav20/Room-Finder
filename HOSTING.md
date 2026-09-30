# אירוח RoomFinder על מחשב ישן בבית

המדריך מעביר את RoomFinder מהמחשב הראשי (Windows עם Docker Desktop) למחשב ישן שעובד בבית 24/7, בלי מסך. בסוף התהליך:

- המעקב רץ כל הזמן, גם כשהמחשב הראשי כבוי.
- הדשבורד נפתח מהטלפון ומהמחשב, גם מחוץ לבית, בכתובת `https://roomfinder.<הרשת-שלך>.ts.net`.
  - הגישה היא דרך [Tailscale](https://tailscale.com): חיבור מוצפן, בלי לפתוח שום פורט בראוטר.
  - רק מכשירים שחיברת בעצמך יכולים להגיע אליו.
- ההתראות בטלגרם, המשתמשים, המודעות וההגדרות עוברים כמו שהם.

**למה בבית ולא בענן:** יד2 ומדלן חוסמים כתובות של שרתי ענן. מחשב בבית גולש מהכתובת הביתית, בדיוק כמו היום.

```
טלפון / מחשב ──(Tailscale, מוצפן)──▶ השרת בבית: tailscale serve (HTTPS) ──▶ 127.0.0.1:8000 ──▶ RoomFinder
                                                │
                                                └──(האינטרנט הביתי)──▶ יד2, מדלן, קומו... וטלגרם
```

## Ubuntu Server או Windows?

בשני המסלולים רצה אותה מערכת בדיוק (ב-Docker), עם אותה גישה דרך Tailscale.

| | Ubuntu Server (מומלץ) | Windows עם Docker Desktop |
|---|---|---|
| **אחרי הפסקת חשמל או עדכון** | חוזר לבד | חוזר אחרי כניסה אוטומטית ל-Windows |
| **זיכרון** | 4GB מספיקים | לפחות 8GB |
| **עדכוני אבטחה** | עד 2031 (גרסה 26.04 LTS) | Windows 11: כן. Windows 10: כבר לא, אלא אם נרשמת להארכה, שנגמרת באוקטובר 2026 |
| **מה שיש היום על המחשב** | נמחק בהתקנה | נשאר |
| **ניהול מרחוק** | SSH | Remote Desktop (ב-Windows Pro) או SSH |
| **היכרות** | חדש | כמו במחשב הראשי |

- **מסלול Ubuntu Server:** שלבים 1-11 כאן למטה.
- **מסלול Windows:** [בסוף המדריך](#מסלול-windows). מתאים אם במחשב הישן יש Windows 11 ולא רוצים למחוק אותו.

**בלי מחשב נוסף, על דומיין משלך:** אפשר גם להשאיר את המערכת במחשב הראשי, ולפתוח את הדשבורד בכתובת כמו `https://roomfinder.example.com`. ראו [דומיין משלך עם Cloudflare Tunnel](#דומיין-משלך-עם-cloudflare-tunnel). המחיר: כשהמחשב כבוי או ישן, המעקב והדשבורד לא עובדים.

## מה צריך

- **המחשב הישן:**
  - **מעבד:** Intel או AMD של 64 ביט. המערכת משתמשת ב-Google Chrome ללינוקס, שקיים רק למעבדים כאלה, כך שלא Raspberry Pi.
  - **זיכרון:** לפחות 4GB ב-Ubuntu, ולפחות 8GB ב-Windows. המערכת עצמה צורכת כ-1.2GB.
  - **דיסק:** לפחות 40GB פנויים, ועדיף SSD. ה-image לבדו תופס כ-5GB, ובזמן הבנייה צריך בערך פי שניים.
  - **רשת:** עדיף בכבל לראוטר, אבל Wi-Fi גם עובד.
- במסלול Ubuntu: דיסק און קי של 8GB לפחות, לקובץ ההתקנה.
- מסך ומקלדת, רק לזמן ההתקנה.
- שעה-שעתיים.

> ⚠️ במסלול Ubuntu, ההתקנה מוחקת את כל מה שיש על הדיסק של המחשב הישן. לגבות ממנו קודם כל מה שחשוב.

> 🔑 אחרי המעבר הדשבורד נגיש מהטלפון ומכל מקום (רק למכשירים שלך). אם סיסמת המנהל חלשה, להחליף אותה עכשיו בדף **החשבון שלי**.

> 📤 השרת מוריד את הקוד מ-GitHub, אז לפני שמתחילים לוודא שכל השינויים נדחפו (`git push` מהמחשב הראשי).

בפקודות שבהמשך צריך להחליף את המילים האלה:

| במקום | לכתוב |
|---|---|
| `USER` | שם המשתמש ל-Linux שבוחרים בהתקנה (שלב 1) |
| `SERVER_IP` | הכתובת של השרת ברשת הביתית, למשל `10.0.0.23` |
| `TAILNET` | שם הרשת שלך ב-Tailscale, למשל `tail1a2b3c.ts.net` (שלב 5) |

---

## 1. התקנת Ubuntu Server

1. להוריד את **Ubuntu Server 26.04 LTS** מ-https://ubuntu.com/download/server. גם 24.04 LTS מתאים.
2. לצרוב את הקובץ לדיסק און קי עם [Rufus](https://rufus.ie) או [balenaEtcher](https://etcher.balena.io).
3. לחבר את הדיסק און קי למחשב הישן, להדליק, ולבחור לעלות ממנו. תפריט האתחול נפתח ב-F12, F11 או Esc, תלוי ביצרן.
4. במסכי ההתקנה:
   - **Network:** בכבל אין צורך לגעת. ב-Wi-Fi בוחרים רשת ומקלידים סיסמה.
   - **Storage:** להשאיר את *Use an entire disk*.
   - **Profile:**
     - **Your server's name:** `roomfinder`. השם הזה יהיה חלק מכתובת הדשבורד. כתובות כאלה נרשמות ביומני תעודות ציבוריים, אז לא לשים בו שם פרטי.
     - **Username:** למשל `dor`. זה ה-`USER`.
     - **Password:** סיסמה חזקה.
   - **SSH:** לסמן *Install OpenSSH server*.
     - אם יש לך מפתח SSH ב-GitHub, אפשר לבחור *Import SSH identity → from GitHub* ולדלג אחר כך על החלק של המפתח בשלב 2.
   - **Featured server snaps:** לא לבחור כלום, ובמיוחד לא `docker`. את Docker הרשמי מתקינים בשלב 4.
5. בסוף: *Reboot Now*, ולהוציא את הדיסק און קי.
6. להתחבר במסך עם שם המשתמש והסיסמה, ולבדוק:
   ```bash
   uname -m      # צריך להיות x86_64
   free -h       # זיכרון
   df -h /       # מקום פנוי
   hostname -I   # הכתובת ברשת הביתית: זה SERVER_IP
   ```
   ההתקנה משאירה לפעמים חלק מהדיסק לא בשימוש. אם `df` מראה הרבה פחות מגודל הדיסק:
   ```bash
   sudo lvextend -r -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
   ```
7. מומלץ: בהגדרות הראוטר לשריין לשרת כתובת קבועה (*DHCP reservation*), כדי ש-`SERVER_IP` לא ישתנה.

מכאן אפשר לנתק את המסך והמקלדת, וכל השאר נעשה מהמחשב הראשי.

## 2. התחברות מהמחשב הראשי (SSH)

ב-PowerShell במחשב הראשי:

```powershell
# פעם אחת: יצירת מפתח SSH (אם עוד אין). Enter לשאלות, ורצוי לבחור passphrase
ssh-keygen -t ed25519
# העתקת המפתח לשרת (זו הפעם האחרונה שמקלידים את הסיסמה של השרת)
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh USER@SERVER_IP "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
# מעכשיו נכנסים בלי סיסמה
ssh USER@SERVER_IP
```

כל הפקודות עד שלב 6.2 רצות בשרת, בתוך חיבור ה-SSH.

## 3. עדכונים והקשחה

```bash
sudo apt update && sudo apt full-upgrade -y
sudo timedatectl set-timezone Asia/Jerusalem
```

**עדכוני אבטחה אוטומטיים** כבר פועלים ב-Ubuntu Server. כדי שהשרת גם יאתחל את עצמו בלילה כשעדכון דורש את זה (המערכת חוזרת לבד):

```bash
sudo tee /etc/apt/apt.conf.d/52automatic-reboot >/dev/null <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
EOF
```

**SSH רק עם מפתח.** קודם לוודא שהכניסה בלי סיסמה משלב 2 עובדת, ורק אז:

```bash
sudo tee /etc/ssh/sshd_config.d/99-roomfinder.conf >/dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
sudo systemctl restart ssh
```

לפני שסוגרים את החלון הקיים, לבדוק מחלון PowerShell חדש ש-`ssh USER@SERVER_IP` עדיין עובד.

**חומת אש.** לא לקבל שום חיבור מבחוץ, חוץ מ-SSH מהרשת הביתית ומ-Tailscale:

```bash
ip -4 route | grep 'proto kernel'   # הרשת הביתית, למשל 10.0.0.0/24 או 192.168.1.0/24
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 10.0.0.0/24 to any port 22 proto tcp   # להחליף ברשת מהשורה הראשונה
sudo ufw allow in on tailscale0
sudo ufw enable
```

## 4. Docker

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
exit
```

להתחבר שוב (`ssh USER@SERVER_IP`), כדי שההרשאה החדשה תיכנס לתוקף, ולבדוק:

```bash
docker run --rm hello-world
docker compose version
```

- Docker עולה לבד בכל הדלקה של השרת.
- משתמש בקבוצת `docker` הוא בפועל root בשרת. זה בסדר כשרק אתה מתחבר אליו.

## 5. Tailscale

1. לפתוח חשבון חינמי ב-https://tailscale.com, עם כניסה דרך Google, GitHub או Microsoft.
2. בשרת:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
   הפקודה מדפיסה קישור. פותחים אותו במחשב הראשי ומאשרים את השרת.
3. **תוקף המפתח:** בלוח הבקרה, ב-https://login.tailscale.com/admin/machines, ליד `roomfinder` בוחרים **⋯ ← Disable key expiry**. בלי זה השרת מתנתק מהרשת אחרי כחצי שנה.
4. **HTTPS:** בדף DNS בלוח הבקרה (https://login.tailscale.com/admin/dns):
   - לוודא ש-*MagicDNS* פועל.
   - להפעיל את *HTTPS Certificates*.
   - שם הרשת (`TAILNET`) מופיע בראש הדף, למשל `tail1a2b3c.ts.net`.
5. להתקין את אפליקציית Tailscale בטלפון ובמחשב הראשי, ולהתחבר עם אותו חשבון.

אם השרת נקרא אחרת ולא `roomfinder`, השם בכתובת יהיה השם שלו. אפשר לשנות אותו עם `sudo tailscale set --hostname=roomfinder`.

## 6. העברת המערכת

### 6.1 הקוד (בשרת)

המאגר ב-GitHub פרטי. לשרת נותנים מפתח משלו (*Deploy key*), שיכול רק לקרוא, ורק את המאגר הזה:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/roomfinder_deploy -N "" -C "roomfinder server"
cat ~/.ssh/roomfinder_deploy.pub
```

ב-GitHub: המאגר ← **Settings ← Deploy keys ← Add deploy key**. מדביקים את השורה שהודפסה, **בלי** לסמן *Allow write access*.

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-roomfinder
  HostName github.com
  User git
  IdentityFile ~/.ssh/roomfinder_deploy
  IdentitiesOnly yes
EOF
git clone git@github-roomfinder:doriav20/Room-Finder.git ~/RoomFinder
```

בחיבור הראשון git שואל אם לסמוך על המפתח של github.com. להשוות את הטביעה לזו שב[דף של GitHub](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints), ולענות `yes`.

### 6.2 עצירה וגיבוי (במחשב הראשי)

מכאן והלאה המערכת לא רצה בשני מחשבים באותו זמן:
- שני בוטים עם אותו token מתנגשים בטלגרם, והודעות הולכות לאיבוד.
- כל אתר נבדק פעמיים.

ב-PowerShell, מתיקיית הפרויקט:

```powershell
cd D:\Projects\RoomFinder
docker compose stop
docker run --rm -v roomfinder_roomfinder-data:/data -v ${PWD}:/backup alpine tar czf /backup/roomfinder-backup.tgz /data
docker compose down
scp roomfinder-backup.tgz USER@SERVER_IP:~/
scp .env USER@SERVER_IP:~/RoomFinder/.env
```

- **`docker compose down`, בלי `-v`:** הנתונים נשארים גם במחשב הראשי, כגיבוי. הקונטיינר עצמו נמחק, כך שהוא לא יעלה שוב לבד כש-Docker Desktop נפתח.
- **הגיבוי ו-`.env` רגישים:** `roomfinder-backup.tgz` מכיל את המשתמשים ואת חיבורי הטלגרם, ו-`.env` מכיל את ה-token של הבוט. לא לשלוח אותם לשום מקום מחוץ לבית.

### 6.3 הגדרה והפעלה (בשרת)

```bash
cd ~/RoomFinder
sed -i 's/\r$//' .env   # סופי שורות של Windows
chmod 600 .env ~/roomfinder-backup.tgz
nano .env
```

להוסיף ל-`.env`, עם שם הרשת שלך:

```
DASHBOARD_URL=https://roomfinder.TAILNET
ALLOWED_HOSTS=["roomfinder.TAILNET"]
COOKIE_SECURE=true
```

- `DASHBOARD_URL`: הכתובת שנכנסת לקישורי הזמנה ולקישורים בהודעות הטלגרם.
- `ALLOWED_HOSTS`: השמות שהדשבורד עונה להם. `localhost` מותר תמיד.
- `COOKIE_SECURE`: עוגיית ההתחברות נשלחת רק ב-HTTPS.

לשמירה ב-nano: Ctrl+O ואז Enter. ליציאה: Ctrl+X.

בנייה, שחזור הנתונים והפעלה:

```bash
docker compose build     # בפעם הראשונה 10-30 דקות במחשב ישן
docker compose create    # יוצר את ה-volume ואת הקונטיינר, בלי להפעיל
docker run --rm -v roomfinder_roomfinder-data:/data -v ~/roomfinder-backup.tgz:/backup.tgz:ro alpine tar xzf /backup.tgz -C /
docker compose up -d
docker compose logs -f app   # Ctrl+C כדי לצאת
```

בלוגים צריך לראות `Telegram bot @... is up` ואת `Scheduler started`.

### 6.4 הדשבורד ב-HTTPS

```bash
sudo tailscale serve --bg 8000
tailscale serve status
```

- אם הפקודה מדפיסה קישור להפעלת Serve או HTTPS ברשת, פותחים אותו, מאשרים, ומריצים אותה שוב.
- `--bg` שומר את ההגדרה גם אחרי הפעלה מחדש.
- הדשבורד עצמו מחובר רק ל-127.0.0.1 בשרת, ומבחוץ מגיעים אליו רק דרך Tailscale. לא לשנות את `127.0.0.1:8000:8000` ב-`compose.yaml`: Docker עוקף את ufw, ופורט פתוח היה נגיש לכל הרשת הביתית.

### 6.5 בדיקה

- **דשבורד:** בטלפון, עם Tailscale מחובר, לפתוח את `https://roomfinder.TAILNET`. אמור להופיע דף כניסה עם מנעול תקין. להתחבר.
- **מקורות:** כל האתרים ירוקים אחרי הבדיקה הבאה. כמנהל, **פתח את הדפדפן של המערכת** מציג את Chrome של השרת.
- **טלגרם:** לשלוח לבוט `/status`.
- **הדלקה מחדש:** להריץ `sudo reboot`. אחרי 2-3 דקות הכול חוזר לבד: Docker, הקונטיינר, Tailscale והכתובת ב-HTTPS.
- **ניקוי:** בשרת `rm ~/roomfinder-backup.tgz`. במחשב הראשי למחוק את `roomfinder-backup.tgz` מתיקיית הפרויקט.

## 7. חשמל והדלקה מחדש

- **BIOS:** להגדיר את *Restore on AC Power Loss* (או *After Power Failure*) ל-*Power On*. כך המחשב נדלק לבד אחרי הפסקת חשמל.
- **שינה:** Ubuntu Server לא נכנס לשינה.
- **מחשב נייד:** להשאיר אותו בחשמל, ולוודא שסגירת המכסה לא מרדימה אותו:
  ```bash
  sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/; s/^#\?HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/' /etc/systemd/logind.conf
  sudo systemctl restart systemd-logind
  ```
  אם ב-BIOS יש הגבלת טעינה (למשל עד 80%), כדאי להפעיל אותה, כדי שהסוללה תישחק פחות.

## 8. תחזוקה

**עדכון קוד:** מפתחים ודוחפים מהמחשב הראשי, ובשרת מריצים:

```bash
cd ~/RoomFinder && git pull && docker compose up -d --build
docker image prune -f   # מפנה את ה-images הישנים
```

**מצב ולוגים:** `docker compose ps`, `docker compose logs -f app`, או `/status` בטלגרם. הלוגים מוגבלים ל-50MB.

**גיבוי לילי** של מסד הנתונים, בלי לעצור את המערכת. הגיבויים נשמרים 30 יום.

```bash
mkdir -p ~/backups
crontab -e
```

ולהוסיף את השורה:

```
30 4 * * * docker exec roomfinder python -c "import sqlite3; sqlite3.connect('/data/roomfinder.db').backup(sqlite3.connect('/data/backup.db'))" && docker cp roomfinder:/data/backup.db ~/backups/roomfinder-$(date +\%F).db && find ~/backups -name 'roomfinder-*.db' -mtime +30 -delete
```

מדי פעם כדאי להעתיק גיבויים גם למחשב הראשי, כי דיסק ישן יכול להתקלקל:

```powershell
scp USER@SERVER_IP:~/backups/roomfinder-*.db D:\Backups\
```

**שחזור מגיבוי** (עם התאריך של הקובץ):

```bash
cd ~/RoomFinder
docker compose stop
docker run --rm -v roomfinder_roomfinder-data:/data -v ~/backups:/backup alpine sh -c "cp /backup/roomfinder-2026-10-01.db /data/roomfinder.db && rm -f /data/roomfinder.db-wal /data/roomfinder.db-shm"
docker compose start
```

## 9. פיתוח במחשב הראשי אחרי המעבר

- ב-`.env` של המחשב הראשי להשאיר את `TELEGRAM_BOT_TOKEN=` ריק, או ליצור בוט נפרד לבדיקות. אחרת הוא מתנגש עם הבוט שבשרת.
- להריץ שם רק במצב פיתוח (`compose.dev.yaml`), שבו הבדיקות האוטומטיות של האתרים כבויות.
- אחרי כמה ימים שהשרת עובד טוב, אפשר למחוק במחשב הראשי את הנתונים הישנים: `docker volume rm roomfinder_roomfinder-data`.

## 10. אנשים נוספים

- כדי להיכנס לדשבורד צריך Tailscale:
  1. בלוח הבקרה, ליד השרת, בוחרים **⋯ ← Share** ושולחים את הקישור.
  2. האדם פותח חשבון Tailscale משלו, ורואה רק את השרת הזה.
  3. אחר כך שולחים לו קישור הזמנה מדף **משתמשים** בדשבורד.
- לא להשתמש ב-*Tailscale Funnel* ולא לפתוח פורט בראוטר. שתי האפשרויות חושפות את הדשבורד לכל האינטרנט. מי שרוצה גישה בלי Tailscale: [דומיין משלך עם Cloudflare Tunnel](#דומיין-משלך-עם-cloudflare-tunnel), עם Cloudflare Access.
- ההתראות בטלגרם מגיעות גם בלי Tailscale. צריך אותו רק כדי לפתוח את הדשבורד.

## 11. תקלות נפוצות

| מה רואים | מה לעשות |
|---|---|
| `Invalid host header` | הכתובת לא מופיעה ב-`ALLOWED_HOSTS`. לתקן את `.env` ולהריץ `docker compose up -d`. |
| ההתחברות לא נשמרת וחוזרים לדף הכניסה | עם `COOKIE_SECURE=true` צריך לפתוח את הדשבורד ב-`https://`, לא ב-`http://`. |
| הדף לא נפתח בטלפון | לבדוק ש-Tailscale בטלפון מחובר. בשרת: `tailscale status` ו-`tailscale serve status`. |
| `Conflict: terminated by other getUpdates request` בלוגים | הבוט רץ גם במקום אחר, כנראה במחשב הראשי. לעצור אותו שם, או לרוקן שם את ה-token. |
| יד2 או מדלן חוסמים הרבה | לוודא שאין VPN או *Exit Node* בשרת (`tailscale status`), כדי שהגלישה תצא מהכתובת הביתית. בקפצ'ה: **מקורות ← פתח את הדפדפן של המערכת**. |
| הדיסק מתמלא | `docker system df`, ואז `docker image prune -f` ו-`docker builder prune -f`. |
| שכחת את סיסמת המנהל | בשרת: `cd ~/RoomFinder && docker compose exec app python -m roomfinder reset-link USERNAME` (עם שם המשתמש בדשבורד). |

## צ'קליסט סיום

- [ ] `https://roomfinder.TAILNET` נפתח מהטלפון ומהמחשב, עם מנעול תקין.
- [ ] הבוט עונה ל-`/status` והמקורות ירוקים.
- [ ] אחרי `sudo reboot` הכול חוזר לבד.
- [ ] המערכת לא רצה יותר במחשב הראשי (`docker compose ps` שם ריק).
- [ ] *Key expiry* כבוי לשרת ב-Tailscale.
- [ ] הגיבוי הלילי מוגדר (`crontab -l`).
- [ ] סיסמת המנהל חזקה.

---

## מסלול Windows

במסלול הזה המחשב הישן מריץ בדיוק את מה שרץ היום במחשב הראשי: Windows עם Docker Desktop. יש שני הבדלים מול שרת Ubuntu:
- Docker Desktop רץ רק כשמשתמש מחובר ל-Windows. לכן מגדירים כניסה אוטומטית, ונעילה אוטומטית מיד אחריה.
- אחרי עדכוני Windows המחשב מאתחל את עצמו. בערך 3 דקות אחר כך הכול חוזר.

### לפני שמתחילים

- **גרסה:** בודקים עם `winver`.
  - Windows 11 מתאים.
  - Windows 10 כבר לא מקבל עדכוני אבטחה, אלא אם נרשמת להארכה (שנגמרת באוקטובר 2026). מחשב שרץ כל הזמן ברשת צריך עדכונים, אז במקרה כזה עדיף לשדרג ל-Windows 11 אם המחשב מתאים, או לעבור למסלול Ubuntu.
- **זיכרון:** לפחות 8GB.
- **וירטואליזציה:** במנהל המשימות ← ביצועים ← CPU צריך להופיע *Virtualization: Enabled*. אם היא כבויה, מפעילים ב-BIOS (Intel VT-x, או AMD-V/SVM).
- **חשבון:** משתמש עם סיסמה חזקה. עדיף חשבון מקומי ולא חשבון Microsoft, כי כך הכניסה האוטומטית (שלב W6) פשוטה יותר.

### W1. הכנת Windows

1. **עדכונים:** הגדרות ← Windows Update. להתקין הכול ולאתחל.
2. **שם המחשב:** לשנות ל-`roomfinder` (הגדרות ← מערכת ← אודות ← שנה שם למחשב זה), ולאתחל. השם יהיה חלק מכתובת הדשבורד. כמו בשלב 1, לא לשים בו שם פרטי.
3. **חשמל:** ב-PowerShell כמנהל:
   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   powercfg /hibernate off
   # במחשב נייד: סגירת המכסה כשהוא בחשמל לא עושה כלום
   powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
   powercfg /setactive SCHEME_CURRENT
   ```
4. **BIOS:** שיידלק לבד אחרי הפסקת חשמל, כמו בשלב 7.

### W2. Docker Desktop

```powershell
winget install -e --id Docker.DockerDesktop
```

(או מההורדה ב-https://www.docker.com/products/docker-desktop/.)

1. לאתחל כשמתבקשים, לפתוח את Docker Desktop, ולאשר את התנאים. זה קורה פעם אחת.
2. ב-Settings ← General:
   - לסמן *Start Docker Desktop when you sign in to your computer*.
   - לבטל את *Open Docker Dashboard when Docker Desktop starts*.
3. ב-Settings ← Software updates: לבטל הורדה אוטומטית של עדכונים. עדכון של Docker Desktop מחכה לפעמים ללחיצה, אז עדיף לעדכן ידנית כשנמצאים ליד המחשב.

### W3. Git והקוד

```powershell
winget install -e --id Git.Git
```

לסגור את PowerShell ולפתוח מחדש (כדי ש-git יוכר), ואז:

```powershell
New-Item -ItemType Directory -Force -Path $HOME\.ssh | Out-Null
ssh-keygen -t ed25519 -f $HOME\.ssh\roomfinder_deploy   # Enter פעמיים: בלי passphrase
Get-Content $HOME\.ssh\roomfinder_deploy.pub
```

1. להוסיף את השורה שהודפסה כ-Deploy key ב-GitHub, בלי הרשאת כתיבה, בדיוק כמו בשלב 6.1.
2. להוריד את הקוד:
   ```powershell
   Add-Content -Path $HOME\.ssh\config -Value "Host github-roomfinder`n  HostName github.com`n  User git`n  IdentityFile ~/.ssh/roomfinder_deploy`n  IdentitiesOnly yes"
   git clone git@github-roomfinder:doriav20/Room-Finder.git $HOME\RoomFinder
   ```
3. כשנשאלים על המפתח של github.com, להשוות לטביעה ב[דף של GitHub](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).

### W4. Tailscale

1. להתקין מ-https://tailscale.com/download/windows (או `winget install -e --id tailscale.tailscale`) ולהתחבר.
2. באייקון של Tailscale ליד השעון: *Preferences* ← לסמן *Run unattended*, כדי שיעבוד גם בלי משתמש מחובר.
3. בלוח הבקרה: *Disable key expiry* לשרת, והפעלת *HTTPS Certificates*, כמו בשלב 5 (סעיפים 3-4).
4. להתקין את Tailscale גם בטלפון ובמחשב הראשי (שלב 5, סעיף 5).

### W5. העברת המערכת

1. **במחשב הראשי:** לעצור ולגבות לפי שלב 6.2, בלי שתי שורות ה-`scp`.
2. **העתקה:** להעתיק את `roomfinder-backup.tgz` ואת `.env` לתיקייה `RoomFinder` שבתיקיית המשתמש במחשב הישן. הכי פשוט בדיסק און קי, ולמחוק ממנו אחר כך. לא דרך ענן (OneDrive וכדומה), כי ב-`.env` נמצא ה-token של הבוט.
3. **במחשב הישן:** להוסיף ל-`.env` את שלוש השורות משלב 6.3, עם `notepad $HOME\RoomFinder\.env`.
4. ב-PowerShell:
   ```powershell
   cd $HOME\RoomFinder
   docker compose build     # בפעם הראשונה 10-30 דקות
   docker compose create
   docker run --rm -v roomfinder_roomfinder-data:/data -v ${PWD}:/backup alpine tar xzf /backup/roomfinder-backup.tgz -C /
   docker compose up -d
   docker compose logs -f app   # Ctrl+C כדי לצאת
   tailscale serve --bg 8000
   ```
   אם `tailscale serve` לא מצליח, להריץ אותו ב-PowerShell כמנהל.
5. **ניקוי:** למחוק את `roomfinder-backup.tgz` מהמחשב הישן ומהדיסק און קי.

### W6. כניסה אוטומטית ונעילה

1. **כניסה אוטומטית:** להוריד את [Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon) של Microsoft Sysinternals, להריץ, למלא שם משתמש וסיסמה, וללחוץ *Enable*. הסיסמה נשמרת מוצפנת, לא כטקסט.
   - עם חשבון Microsoft ייתכן שצריך קודם לכבות את *For improved security, only allow Windows Hello sign-in* (הגדרות ← חשבונות ← אפשרויות כניסה).
2. **נעילה מיד אחרי הכניסה**, כדי שמי שניגש למקלדת לא יקבל מחשב פתוח. Docker Desktop ממשיך לרוץ גם כשהמסך נעול. ב-PowerShell כמנהל:
   ```powershell
   $action = New-ScheduledTaskAction -Execute "rundll32.exe" -Argument "user32.dll,LockWorkStation"
   $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
   $trigger.Delay = "PT1M"
   Register-ScheduledTask -TaskName "RoomFinder - lock after sign-in" -Action $action -Trigger $trigger
   ```
3. **בדיקה:** לאתחל את המחשב. אחרי כ-3 דקות המסך נעול, הדשבורד נפתח מהטלפון, והבוט עונה ל-`/status`.

### W7. ניהול מרחוק (לא חובה)

- **Windows Pro:** הגדרות ← מערכת ← Remote Desktop ← מופעל. מהמחשב הראשי מתחברים עם `mstsc /v:roomfinder`, דרך Tailscale.
- **Windows Home:** אין בו Remote Desktop. לשורת פקודה אפשר להפעיל OpenSSH Server, ב-PowerShell כמנהל:
  ```powershell
  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
  Set-Service -Name sshd -StartupType Automatic
  Start-Service sshd
  ```
  ומהמחשב הראשי: `ssh USER@roomfinder`.

### W8. תחזוקה

**עדכון קוד:**

```powershell
cd $HOME\RoomFinder; git pull; docker compose up -d --build; docker image prune -f
```

**גיבוי לילי:** הסקריפט `scripts\backup.ps1` מגבה את מסד הנתונים בלי לעצור את המערכת. הוא שומר עותק לכל יום בתיקייה `RoomFinderBackups` שבתיקיית המשתמש, ומוחק עותקים בני יותר מ-30 יום. להפעלה כל לילה ב-04:30:

```powershell
$script = "$HOME\RoomFinder\scripts\backup.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At 4:30am
Register-ScheduledTask -TaskName "RoomFinder - nightly backup" -Action $action -Trigger $trigger
Start-ScheduledTask -TaskName "RoomFinder - nightly backup"   # הרצת בדיקה עכשיו
```

אחרי הרצת הבדיקה צריך להופיע בתיקייה `RoomFinderBackups` קובץ עם התאריך של היום. מדי פעם כדאי להעתיק גיבויים גם למחשב אחר.

**שחזור מגיבוי** (עם התאריך של הקובץ):

```powershell
cd $HOME\RoomFinder
docker compose stop
docker run --rm -v roomfinder_roomfinder-data:/data -v "$HOME\RoomFinderBackups:/backup" alpine sh -c "cp /backup/roomfinder-2026-10-01.db /data/roomfinder.db && rm -f /data/roomfinder.db-wal /data/roomfinder.db-shm"
docker compose start
```

**עדכוני Windows ו-Docker Desktop:**
- Windows מתעדכן ומאתחל לבד, בדרך כלל בלילה. אחרי האתחול הכול חוזר.
- את Docker Desktop מעדכנים ידנית פעם בכמה חודשים.

**מה עוד חל גם על Windows:** שלב 7 (BIOS), שלב 9 (פיתוח במחשב הראשי), שלב 10 (אנשים נוספים), טבלת התקלות בשלב 11 (את פקודות השרת מריצים ב-PowerShell), והצ'קליסט.

> **ב-Windows Pro יש עוד אפשרות:** להריץ את מסלול Ubuntu בתוך מכונה וירטואלית של Hyper-V שמוגדרת לעלות עם המחשב (*Automatic Start Action: Always start*). כך אין צורך בכניסה אוטומטית, אבל צריך יותר זיכרון: לפחות 8GB, ועדיף 16GB.

---

## דומיין משלך עם Cloudflare Tunnel

המסלול הזה פותח את הדשבורד בכתובת על דומיין שלך, למשל `https://roomfinder.example.com`, מכל מכשיר ובלי אפליקציה. הוא עובד בכל מחשב שמריץ את המערכת: המחשב הראשי, או השרת מהמסלולים שלמעלה.

```
טלפון / מחשב ──(HTTPS)──▶ Cloudflare ◀──(חיבור יוצא)── קונטיינר tunnel ──▶ app:8000 ──▶ RoomFinder
```

- **בלי לגעת בראוטר:** הקונטיינר `tunnel` יוצר חיבור **יוצא** ל-Cloudflare, כמו כל גלישה רגילה. לא פותחים פורט, וכתובת ה-IP של הבית לא נחשפת ולא משנה.
- **עולה ויורד עם המערכת:** `docker compose up -d` מעלה גם אותו, ו-`docker compose down` מוריד גם אותו. כשהמערכת למטה, מי שנכנס לכתובת רואה דף שגיאה של Cloudflare.
- **הסריקות לא משתנות:** רק התעבורה אל הדשבורד עוברת דרך Cloudflare. יד2 ומדלן ממשיכים לראות את הכתובת הביתית.

**מה צריך לדעת:**
- **ה-DNS של כל הדומיין עובר לניהול ב-Cloudflare.** הדומיין נשאר רשום אצל הרשם (החברה שממנה קנית אותו), אבל אם יש לו רשומות אחרות, כמו מייל או אתר, צריך לוודא שהן הועתקו ל-Cloudflare (סעיף T1).
- **Cloudflare יושב באמצע:** הם מפענחים את התעבורה לדשבורד, כי הם אלה שמספקים את ה-HTTPS.
- **הדשבורד נגיש לכל האינטרנט.** לכן סעיף T4 (Cloudflare Access) הוא חובה, לא המלצה.

### T1. העברת ה-DNS ל-Cloudflare

1. לפתוח חשבון חינמי ב-https://dash.cloudflare.com, ולבחור **Add a domain** (או *Add a site*) עם הדומיין שלך ותוכנית **Free**.
2. Cloudflare סורק את רשומות ה-DNS הקיימות ומציג אותן. להשוות לרשימה בפאנל של הרשם. מה שחסר (בעיקר רשומות `MX` ו-`TXT` של מייל) מוסיפים ידנית.
3. Cloudflare מציג שני **Nameservers**, למשל `ada.ns.cloudflare.com`.
4. **בפאנל של הרשם**, בהגדרות הדומיין, למצוא את *Nameservers* (לפעמים *Name Servers* או *DNS servers*) ולבחור בשרתים מותאמים אישית (*custom*). להחליף את ה-Nameservers הקיימים בשניים של Cloudflare, ולשמור.
5. לחכות למייל מ-Cloudflare שהדומיין פעיל. בדרך כלל זה לוקח פחות משעה, ולפעמים עד יום.

### T2. יצירת המנהרה

1. ב-Cloudflare: **Zero Trust** (בתפריט הצד). בפעם הראשונה בוחרים שם לארגון ואת תוכנית ה-**Free**.
2. **Networks ← Tunnels ← Create a tunnel**, מסוג **Cloudflared**. שם: `roomfinder`.
3. במסך ההתקנה בוחרים **Docker**. מופיעה פקודה שמסתיימת ב-`--token eyJ...`. **לא מריצים אותה.** מעתיקים רק את ה-token, מה שאחרי `--token`.
4. בשלב הבא (*Public Hostname* / *Route traffic*):
   - **Subdomain:** `roomfinder`
   - **Domain:** הדומיין שלך
   - **Service:** סוג `HTTP`, כתובת `app:8000`

   כתובת היעד היא `app:8000` ולא `localhost:8000`: בתוך קונטיינר, `localhost` הוא הקונטיינר עצמו, ו-`app` הוא השם של RoomFinder ברשת של Docker.

### T3. הגדרה והפעלה

ב-`.env`, עם הדומיין שלך וה-token מ-T2:

```
DASHBOARD_URL=https://roomfinder.example.com
ALLOWED_HOSTS=["roomfinder.example.com"]
COOKIE_SECURE=true
COMPOSE_PROFILES=tunnel
CLOUDFLARE_TUNNEL_TOKEN=eyJ...
```

- `COMPOSE_PROFILES=tunnel`: בלי השורה הזו הקונטיינר `tunnel` לא עולה. כך מי שמריץ את הפרויקט בלי מנהרה (פיתוח, או מסלול Tailscale) לא צריך לשנות כלום.
- **ה-token סודי:** מי שמחזיק אותו יכול להגיש תוכן בכתובת שלך. אם הוא דלף: ב-Zero Trust ← Tunnels ← המנהרה ← *Refresh token*, ולהחליף ב-`.env`.
- משתמשים גם ב-Tailscale? אפשר לשים את שתי הכתובות ב-`ALLOWED_HOSTS`: `["roomfinder.example.com", "roomfinder.TAILNET"]`. ב-`DASHBOARD_URL` שמים את זו שבה משתמשים הכי הרבה.

הפעלה:

```powershell
docker compose up -d
docker compose logs -f tunnel   # צריך לראות "Registered tunnel connection". Ctrl+C כדי לצאת
```

בלוח הבקרה של Cloudflare, המנהרה אמורה להופיע כ-**Healthy**.

### T4. Cloudflare Access: רק אתה נכנס

בלי השלב הזה, כל אחד באינטרנט מגיע לדף הכניסה של הדשבורד. עם Access, Cloudflare שואל קודם לכתובת מייל, ושולח אליה קוד. רק כתובות שאישרת עוברות, ורק אחר כך מופיע דף הכניסה של RoomFinder.

1. ב-Zero Trust: **Access ← Applications ← Add an application ← Self-hosted**.
2. **Application name:** `RoomFinder`. **Public hostname:** `roomfinder` בדומיין שלך.
3. **Policy:** שם `Allowed`, פעולה **Allow**, ובכלל **Include ← Emails** לכתוב את כתובות המייל שמותר להן להיכנס (שלך, ושל מי שהזמנת).
4. **Login methods:** להשאיר את **One-time PIN**.
5. **Session duration:** למשל `1 month`, כדי שלא יבקש קוד כל יום.
6. לשמור.

**בדיקה:** בחלון גלישה בסתר לפתוח את `https://roomfinder.example.com`. צריך להופיע קודם מסך של Cloudflare שמבקש מייל, ורק אחרי הקוד דף הכניסה של RoomFinder.

**אנשים נוספים:** מוסיפים את המייל שלהם ל-Policy (Access ← Applications ← RoomFinder ← Policies), ושולחים להם קישור הזמנה מדף **משתמשים** בדשבורד. לא צריך Tailscale.

### T5. אם זה המחשב הראשי

- **שינה:** כשהמחשב נכנס לשינה, גם המעקב נעצר. כדי שירוץ כל עוד המחשב בחשמל: `powercfg /change standby-timeout-ac 0`.
- **מצב פיתוח:** `docker compose -f compose.yaml -f compose.dev.yaml up -d` מעלה גם את המנהרה, כך שהגרסה שבפיתוח היא זו שנגישה בכתובת. כדי לעבוד בלי מנהרה: `docker compose stop tunnel`.

### תקלות

| מה רואים | מה לעשות |
|---|---|
| דף שגיאה של Cloudflare (502 / *Bad gateway*) | המערכת לא רצה. `docker compose ps`, ואם צריך `docker compose up -d`. |
| *Error 1033* | המנהרה לא מחוברת. `docker compose logs tunnel`, ולוודא ש-`COMPOSE_PROFILES=tunnel` וה-token נכונים ב-`.env`. |
| `Invalid host header` | הכתובת לא מופיעה ב-`ALLOWED_HOSTS`. לתקן את `.env` ולהריץ `docker compose up -d`. |
| הכתובת לא נמצאת בכלל | ה-Nameservers עוד לא התעדכנו (T1), או שה-*Public Hostname* לא הוגדר (T2). |
| המייל של הדומיין הפסיק לעבוד | רשומות `MX`/`TXT` לא הועתקו ל-Cloudflare. להעתיק אותן מהפאנל של הרשם (T1). |
