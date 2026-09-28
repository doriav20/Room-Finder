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

## מה צריך

- **המחשב הישן:**
  - **מעבד:** Intel או AMD של 64 ביט. המערכת משתמשת ב-Google Chrome ללינוקס, שקיים רק למעבדים כאלה, כך שלא Raspberry Pi.
  - **זיכרון:** לפחות 4GB, ועדיף 8GB. המערכת עצמה צורכת כ-1.2GB.
  - **דיסק:** לפחות 40GB פנויים, ועדיף SSD. ה-image לבדו תופס כ-5GB, ובזמן הבנייה צריך בערך פי שניים.
  - **רשת:** עדיף בכבל לראוטר, אבל Wi-Fi גם עובד.
- דיסק און קי של 8GB לפחות, לקובץ ההתקנה.
- מסך ומקלדת, רק לזמן ההתקנה.
- שעה-שעתיים.

> ⚠️ ההתקנה מוחקת את כל מה שיש על הדיסק של המחשב הישן. לגבות ממנו קודם כל מה שחשוב.

> 🔑 אחרי המעבר הדשבורד נגיש מהטלפון ומכל מקום (רק למכשירים שלך). אם סיסמת המנהל חלשה, להחליף אותה עכשיו בדף **החשבון שלי**.

> 📤 השרת מוריד את הקוד מ-GitHub, אז לפני שמתחילים לוודא שכל השינויים נדחפו (`git push` מהמחשב הראשי).

בפקודות שבהמשך צריך להחליף את המילים האלה:

| במקום | לכתוב |
|---|---|
| `USER` | שם המשתמש ל-Linux שבוחרים בהתקנה (שלב 1) |
| `SERVER_IP` | הכתובת של השרת ברשת הביתית, למשל `10.0.0.23` |
| `TAILNET` | שם הרשת שלך ב-Tailscale, למשל `tail1a2b3c.ts.net` (שלב 5) |

**למה Ubuntu Server ולא Windows:** הוא חינמי וקל, ורץ בלי שמישהו מחובר אליו. הוא גם לא נתקע על מסך התחברות אחרי עדכון. Docker Desktop ב-Windows, לעומת זאת, עולה רק אחרי כניסה של משתמש. אם בכל זאת רוצים להשאיר Windows, ראו [חלופה: Windows](#חלופה-להשאיר-את-windows) בסוף.

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
- לא להשתמש ב-*Tailscale Funnel* ולא לפתוח פורט בראוטר. שתי האפשרויות חושפות את הדשבורד לכל האינטרנט.
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

## חלופה: להשאיר את Windows

אפשר, אבל פחות אמין לאורך זמן:

1. להתקין Docker Desktop ולהריץ את המערכת לפי ה-README.
2. בהגדרות של Docker Desktop לסמן *Start Docker Desktop when you sign in*.
3. להגדיר ב-Windows כניסה אוטומטית, כי בלי משתמש מחובר Docker Desktop לא עולה. זה פוגע באבטחה של המחשב עצמו.
4. להגדיר שהמחשב לא ייכנס לשינה לעולם.
5. להתקין Tailscale ל-Windows, ובחלון PowerShell כמנהל להריץ `tailscale serve --bg 8000`.
6. להוסיף ל-`.env` את אותן שלוש שורות משלב 6.3.
7. להעביר את הנתונים לפי שלבים 6.2-6.3, עם פקודות ה-PowerShell שב-README.

אחרי עדכון של Windows המחשב מאתחל את עצמו. המערכת חוזרת רק אחרי שהכניסה האוטומטית מתבצעת.
