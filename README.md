# Bot Telegram ban tai khoan, thanh toan ngan hang tu dong

Bot cho khach chon mot san pham va so luong, chuyen khoan truc tiep hoac mua bang so du, roi nhan thong tin tai khoan trong chat. Tien duoc ghi nhan **tu dong** tu webhook giao dich den cua [SePay](https://docs.sepay.vn/tich-hop-webhooks.html). Khong can admin duyet tung giao dich.

## Luong hoat dong

Khach mo san pham va bam **Mua ngay**, chon nhanh 1, 2, 3, 5, 10 hoac **So khac** de nhap so luong (toi da 10, tuy kho con). San pham can email se hoi dung so email tuong ung, cach nhau bang dau phay. Sau do bot hien tong tien, cho nhap ma giam gia va chon **Chuyen khoan ngan hang** hoac **Thanh toan bang vi**. Bam nut vi se tru tien va giao cac tai khoan ngay. Bam chuyen khoan: bot tao ma `MUA...` va anh VietQR dung tong tien; khach chuyen dung so tien va giu nguyen noi dung trong 30 phut. SePay xac nhan xong, bot giao tai khoan. Tien chuyen thieu duoc ghi vao so du va bot bao so con thieu; co the chuyen them bang cung ma trong thoi han. Tien chuyen du duoc giu trong so du sau khi giao don. Neu don het han, gia/kho/ma giam gia thay doi truoc luc du tien, bot giu tien da nhan trong so du; khach co the mua lai bang vi. Moi lan thanh toan tao mot don hang trong `/orders`; nhieu tai khoan duoc liet ke thanh cac muc trong cung don va ca don co mot yeu cau bao hanh chung. Tin giao tai khoan va chi tiet don hien tung muc trong cung don. Bot chi dat mot nut **Sao chep don hang** de lay toan bo thong tin tai khoan; neu noi dung qua gioi han nut sao chep cua Telegram, bot mo khoi ma hoac file `.txt` cua ca don. Xem ma chuyen khoan cu trong `/payments`.

Admin mo `/admin` > **Cong tien thu nghiem** de chon **Vi admin cua toi**, tim tai khoan bang `@username`/Telegram ID, hoac mo **Nguoi dung** > chon nguoi > **Cong tien thu nghiem**. Chon muc tien co san hoac nhap so khac (toi da 10.000.000d moi lan), xem so du truoc/sau roi bam **Xac nhan cong**. Moi lan cong duoc ghi trong lich su rieng tai man hinh nay va trang nguoi dung; bam lai nut xac nhan cu khong cong hai lan. Day la so du thu nghiem co the dung mua hang trong kho. Khoan nay khong la giao dich ngan hang, khong nam trong tong nap tien that hay bao cao SePay.

Khi mot lan mua gom nhieu tai khoan, bot gui chung trong mot tin nhan, kem mot nut sao chep ca don, nut **Tai file .txt** va mot nut bao hanh chung. Neu noi dung vuot gioi han nut sao chep cua Telegram, nut sao chep mo mot khoi de sao chep ca don; neu ca khoi qua dai, bot gui lai file `.txt`. Bot dong thoi gui mot file `.txt` UTF-8 chua day du tai khoan cua lan mua; file duoc tao trong bo nho va gui rieng cho dung khach, khong luu file ro tren may chu. Khach co the mo `/orders`, chon don va bam **Tai file .txt** de lay lai file bat cu luc nao. Neu noi dung vuot gioi han tin Telegram, tin nhan chi hien ma don va khach xem day du trong file `.txt`. Don chuyen khoan duoc thu lai theo tung lan mua neu Telegram tam loi, khong gui lai tin nhan tai khoan da gui thanh cong khi chi file bi loi. File `.txt` co the tai ve va chua thong tin dang nhap dang ro, nen khach can giu rieng.

San pham giao bang file JSON: admin mo `/admin` > **San pham** > **Them san pham file .json**, nhap ten, gia, mo ta va xac nhan. Trong chi tiet san pham, bam **Nhap file .json** va gui file duoi dang tai lieu Telegram (JSON hop le, goc la object hoac array, UTF-8, toi da 1 MB). Moi file la mot don vi kho; co the gui nhieu file lien tiep, `/cancel` de dung. Bot ma hoa noi dung file trong SQLite, khong luu file ro tren dia. Khi khach thanh toan bang vi hoac chuyen khoan, bot van gui tin xac nhan don nhung noi dung giao hang chi nam trong file `don-hang-ID.json`/`don-chuyen-khoan-ID.json`. Neu don chi co mot file JSON, bot gui lai noi dung JSON goc. Neu don gom nhieu muc, bot gom thanh mot JSON co `order_id` va mang `items`, moi muc chua `id`, `product`, `type` va `content`. Khach co the tai lai tu `/orders`. San pham file khong co nut sao chep noi dung. Admin xem file chua ban/da ban trong muc kho va nhan lai duoi dang file .json. File nhap kho khong tu gui thong bao cho khach theo doi. Tai khoan thong thuong va tuy chon chi giao file cua loai tai khoan van dung `.txt`; file nhap truoc khi ho tro JSON neu co van tai lai theo dinh dang cu.

San pham tai khoan cung co tuy chon giao noi dung **Chi file .txt**: mo `/admin` > **San pham** > chon san pham > **Chi giao bang file .txt**. Khach van nhan tin xac nhan don, nhung thong tin tai khoan va nut sao chep khong hien trong chat; noi dung nam trong file .txt gui kem. Bam **Hien tai khoan + file .txt** de doi lai cho cac don moi. Lua chon duoc ghi vao tung don khi mua, nen doi tuy chon sau do khong lam lo thong tin cua don cu. San pham file JSON luon giao chi bang .json.

Luong nap tien vao vi cu van hoat dong:

1. Khach bam **Nap tien** hoac go `/nap`, chon nhanh 10.000d, 20.000d, 50.000d... hoac **So khac** roi nhap so tien. Co the go thang `/nap 50000`. Bot tao ma noi dung chuyen khoan rieng, vi du `NAPABC123DEF456`.
2. Khach chuyen khoan vao tai khoan ngan hang cau hinh, giu nguyen ma tren. Sau khi nhap so tien, bot gui anh VietQR ngay trong chat neu co `BANK_BIN`.
3. SePay gui webhook khi co tien vao. Bot kiem tra chu ky HMAC va thoi gian, so tai khoan dich, huong giao dich va ma nap. Bot cong **so tien thuc nhan**, khong dua vao so tien khach khai.
4. Moi ID giao dich ngan hang chi duoc xu ly mot lan. Khach nhan thong bao so du moi va co the mua san pham.
5. Khi mua, bot tru tien va xuat mot tai khoan chua ban trong cung mot giao dich SQLite. Khach xem lai qua `/orders`.

Neu noi dung chuyen khoan khong co ma nap hop le, bot khong tu gan tien cho bat ky ai. Giao dich nam trong `/unmatched` de admin xu ly ngoai le. Chuyen lai cung ma nap van duoc cong theo tung giao dich ngan hang rieng biet.

## Ngon ngu khach hang

Khach mo `/menu` va bam **Ngon ngu / Language**, hoac go `/language` (`/lang`). Chon **Tieng Viet** hay **English**; lua chon duoc luu rieng cho tung Telegram ID, ke ca sau khi khoi dong lai. Mac dinh la Tieng Viet. Menu, huong dan, mua hang, nap tien, don hang, bao hanh va thong bao tu dong dung ngon ngu da chon. Ten va mo ta san pham, noi dung thong bao do admin tu soan van giu nguyen van ban admin nhap. Menu quan tri tiep tuc dung Tieng Viet.

## Cai dat tren Windows

Yeu cau Python 3.10+ va bot Telegram tao bang [@BotFather](https://t.me/BotFather).

```powershell
cd D:\AI_App\telegram-account-shop
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Sua `.env`:

- `BOT_TOKEN`: token do BotFather cap.
- `ADMIN_IDS`: Telegram user ID cua ban, nhieu admin cach nhau boi dau phay. Co the xem ID qua [@userinfobot](https://t.me/userinfobot).
- `BANK_NAME`, `BANK_ACCOUNT_NUMBER`, `BANK_ACCOUNT_HOLDER`: thong tin tai khoan ngan hang chinh da lien ket voi SePay. `BANK_ACCOUNT_NUMBER` la so tai khoan chinh trong truong `accountNumber` cua webhook va `account_number` cua API doi soat.
- `BANK_TRANSFER_ACCOUNT_NUMBER`: so tai khoan khach se chuyen den va duoc in trong anh QR. De trong neu khach chuyen thang vao tai khoan chinh. Neu dung VA cua SePay, nhap so VA o day, con `BANK_ACCOUNT_NUMBER` giu so tai khoan chinh; SePay bao VA qua `subAccount` nhung bao so tai khoan chinh qua `accountNumber`. `BANK_BIN` la ma BIN 6 chu so cho VietQR; co the de trong neu khong dung QR.
- `SEPAY_WEBHOOK_SECRET`: Secret Key dat trong cau hinh webhook HMAC-SHA256 cua SePay. Dung khoa dai, ngau nhien va giu bi mat. Server phai dong bo thoi gian (NTP) vi chu ky chi hop le trong 5 phut.
- `SEPAY_API_TOKEN`: API Token tao tai SePay > Cau hinh Cong ty > API Access. Can cho doi soat giao dich; khac Secret Key webhook. De trong neu chua co Token, bot van nhan webhook nhung chua doi soat dinh ky. Khong gui Token qua chat.
- `INVENTORY_ENCRYPTION_KEY`: khoa ma hoa tai khoan trong kho. Tao mot lan bang lenh duoi day, dien ket qua vao `.env` va sao luu khoa cung voi du lieu. Mat khoa se khong doc duoc tai khoan da luu.
- `WEBHOOK_HOST`, `WEBHOOK_PORT`: dia chi/port HTTP noi bo. Mac dinh chi nghe `127.0.0.1:8080`.

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Chay bot:

```powershell
python bot.py
```

`start_bot.bat` se dung `.venv` neu da cai dat theo cac buoc tren.

Bot tao anh VietQR ngay tren may va gui truc tiep trong Telegram, gom so tai khoan nhan, so tien va ma chuyen khoan. Neu Telegram khong nhan anh, bot gui thong tin chuyen khoan bang chu de khach chuyen thu cong. Sau khi doi `BANK_TRANSFER_ACCOUNT_NUMBER` hoac `BANK_ACCOUNT_NUMBER`, phai khoi dong lai bot; anh QR cu da gui cho khach van tro ve tai khoan cu.

## Chay shop bang nut tren Desktop

Nhan dup vao shortcut **Chay shop Telegram** tren Desktop sau moi lan bat may. Shortcut mo `start_shop.bat`; cua so nay kiem tra bot va ngrok moi 5 giay, tu chay lai tien trinh da thoat, va khong mo trung neu bot/ngrok dang chay. Bot va ngrok khong duoc tu dong bat khi Windows khoi dong.

Giu cua so giam sat mo (co the thu nho). Dong cua so hoac bam Ctrl+C se dung giam sat; bot/ngrok dang chay van tiep tuc, nhung se khong tu phuc hoi nua. Bam shortcut lai de tiep tuc giam sat. Neu can xem loi, mo `.runtime\bot-supervised.log` hoac `.runtime\ngrok-supervised.log`. Shortcut su dung URL ngrok co dinh cua `start_ngrok.bat`; neu doi domain, can cap nhat ca hai file.

Neu bot hoac tunnel ngrok khong san sang lien tuc 60 giay, cua so giam sat gui canh bao truc tiep cho admin qua Telegram API, ke ca khi bot chinh dang tat. Bot duoc kiem tra qua `/healthz` noi bo; tunnel duoc kiem tra qua ngrok agent va duong `/healthz` cong khai moi 30 giay. Khi su co keo dai, no nhac toi da moi gio; neu gui that bai, no thu lai sau 5 phut. Sau khi da bao su co, no bao them mot lan khi dich vu phuc hoi. Can giu cua so giam sat mo va may co mang de nhan canh bao.

Cua so giam sat cung bao cho toan bo khach da dung bot (khong gom admin) khi bot chinh mat ket noi tren 60 giay: "Chu shop co viec ban, bot tam thoi khong hoat dong". Khi bot chay lai, khach nhan "Bot da hoat dong". Tin duoc gui lan luot qua Telegram API va luu hang doi trong SQLite; neu phuc hoi khi tin "ban" chua gui het, cac tin chua gui se duoc huy de tranh bao sai. Khi bat giam sat lan dau, no gui mot lan thong bao bot dang hoat dong. Neu may tat, cua so giam sat dong, hoac may mat mang, khong co chuong trinh nao co the bao ngay luc bot mat ket noi; tin chua gui chi duoc thu lai khi may va mang hoat dong. Mat ket noi ngrok ma bot van chay chi bao admin, khong bao bot ban cho khach.

## Canh bao van hanh

Bot gui tin rieng cho cac ID trong `ADMIN_IDS` neu webhook SePay bi tu choi (HTTP 4xx), qua tai (503), xu ly loi (5xx), hoac khong len lich duoc thong bao giao dich. Moi loai loi chi bao toi da 1 lan/gio de tranh spam; log bot van ghi loi chi tiet. Neu request khong toi duoc webhook vi bot/ngrok khong san sang, cua so giam sat se bao su co nhu mo ta o tren. Kiem tra lich su webhook trong SePay de xem cac lan gui that bai.

Sau khi cap nhat ma nguon, dung bot cu bang Ctrl+C trong cua so bot, roi bam shortcut **Chay shop Telegram** de chay phien ban moi. Bam shortcut khi bot cu con dang chay se chi giam sat tien trinh cu, khong tu tai lai ma nguon.

Tac vu sao luu gui canh bao truc tiep qua Telegram API khi tao/kiem tra ban sao that bai, ke ca khi bot chinh dang tat. Ket qua van ghi trong `.runtime\backup.log` va tac vu tra ve loi de Windows nhan biet. Neu may mat mang hoac `BOT_TOKEN`/`ADMIN_IDS` sai, Telegram khong the nhan canh bao; kiem tra log tai may.

Khi mot don lam kho con 2 tai khoan tro xuong, bot bao admin ngay. Bot cung quet kho sau khi bat dau va moi 10 phut de bao san pham dang ban duoc nhap it hang. San pham demo chua tung co tai khoan khong bao dong. Neu gui tin that bai, bot thu lai o lan quet tiep theo. Admin can mo chat rieng voi bot va gui `/start` it nhat mot lan de nhan canh bao.

## Gioi han spam va tai

Bot gioi han thao tac Telegram theo tung khach (toi da 10 thao tac lien tiep, sau do hoi phuc 1 thao tac moi 2 giay) va tren toan bot (40 thao tac lien tiep, sau do 10 thao tac/giay). Khi vuot muc, bot chi bao mot lan trong 30 giay de khong tao them luong tin nhan. Admin duoc mien gioi han thao tac de van quan ly shop khi co spam.

Tao ma nap co gioi han rieng: moi khach cach nhau 15 giay; toan bot toi da 3 yeu cau lien tiep, sau do 1 yeu cau moi 10 giay. Moi khach co toi da 12 ma nap **chua thanh toan** tao trong 1 gio. Yeu cau vuot muc khong tao ma nap hay tai anh QR. Webhook chi xu ly toi da 32 ket noi cung luc, gioi han 5 giay cho ket noi cham, toi da 16 KB/body va van kiem tra chu ky SePay truoc khi ghi co.

Gioi han trong bot giup may khong bi nghe do spam thong thuong; neu co tan cong mang lon, can them bao ve o tang ngrok/duong truyen. Webhook qua tai co the khong nhan duoc giao dich ngay; can kiem tra lich su webhook loi trong SePay va cau hinh gui lai neu SePay ho tro.

## URL webhook co dinh va sao luu

Chay `start_ngrok.bat` de mo tunnel tai domain co dinh `https://mud-sinless-net.ngrok-free.dev`. URL webhook SePay la `https://mud-sinless-net.ngrok-free.dev/webhooks/sepay`. Dung script nay moi khi khoi dong lai ngrok de domain khong doi. Bot va ngrok can cung chay tren may nay de SePay gui giao dich duoc.

Windows Task Scheduler co tac vu `TelegramAccountShop-DailyBackup` chay luc **03:00 moi ngay** theo gio may, va chay bu neu bo lo lich khi nguoi dung dang nhap lai. Tac vu chay bang tai khoan Windows hien tai; tai khoan can dang nhap de tac vu bat dau. Moi lan chay, `backup_shop.py` dung SQLite backup API de chup du lieu ke ca khi bot dang chay, khoi phuc thu vao file tam, kiem tra toan ven, so ban ghi va khoa giai ma kho, roi moi luu ban sao.

Mac dinh file sao luu duoc dat trong `C:\Users\ASUS\Documents\TelegramAccountShopBackups`; co the doi bang `SHOP_BACKUP_DIR` trong `.env`. Nhat ky ket qua o `.runtime\backup.log`. File sao luu chua du lieu khach hang va giao dich, can giu rieng tu va giu `INVENTORY_ENCRYPTION_KEY` de doc lai tai khoan trong kho. Chuong trinh khong tu xoa cac ban sao cu.

Chay thu ngay bang `python backup_shop.py` hoac `Start-ScheduledTask -TaskName TelegramAccountShop-DailyBackup`. Truc tiep phuc hoi du lieu that chi nen lam sau khi dung bot va sao luu ban hien tai; tac vu hang ngay chi khoi phuc thu tren file tam, khong ghi de `shop.sqlite3`.

## Ket noi SePay

1. Ket noi dung tai khoan ngan hang nhan tien trong SePay. Cau hinh webhook cho su kien **co tien vao**, dung chinh tai khoan nay va dinh dang body **JSON**.
2. Trien khai bot tren may chay lien tuc. Cong bo `https://ten-mien-cua-ban/webhooks/sepay` bang reverse proxy HTTPS den `127.0.0.1:8080`. SePay can URL cong khai; chay tren PC tat/mat mang se khong nhan webhook dung luc.
3. Trong SePay, dat URL webhook tren, chon xac thuc **HMAC-SHA256** va luu Secret Key vao `SEPAY_WEBHOOK_SECRET`. Phan hoi hop le cua bot la HTTP 200 voi `{"success": true}`.
4. Chuyen thu **mot khoan nho that** voi ma bot tao, sau do kiem tra `/balance`. Khong gui webhook gia lap vao he thong that vi Secret Key co quyen lam tang so du.

Vi du cau hinh Caddy cho ten mien da tro ve may chu:

```caddyfile
bot.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Khong mo truc tiep HTTP port 8080 ra Internet. Cho phep SePay goi URL HTTPS va giu `.env`, file SQLite va ban sao luu o noi rieng tu. Du lieu tai khoan trong SQLite duoc ma hoa bang `INVENTORY_ENCRYPTION_KEY`; so du va lich su giao dich khong duoc ma hoa. Bot co gang xoa tin nhan `/stock` cua admin sau khi nhap kho.

## Doi soat tien nap SePay

Tao API Token tai **Cau hinh Cong ty > API Access** trong SePay va dien vao `SEPAY_API_TOKEN` cua `.env`, sau do khoi dong lai bot. Token nay khac `SEPAY_WEBHOOK_SECRET`; theo [tai lieu SePay](https://developer.sepay.vn/vi/sepay-api/v2/tao-api-token), API Token hien khong co phan quyen chi doc, nen can giu file `.env` rieng tu. Khong dan Token vao Telegram hoac README.

Khi co Token, bot tu doi soat moi 120 giay qua [API giao dich v1](https://docs.sepay.vn/api-giao-dich.html). API v1 dung ID giao dich dang so nhu webhook hien tai, nen mot giao dich chi duoc cong tien mot lan du webhook va doi soat cung thay. Lan dau bot quet 30 ngay gan nhat; cac lan sau tiep tuc theo ID va quet lai mot doan ngan de xu ly giao dich den cham. Bot loc giao dich theo `BANK_ACCOUNT_NUMBER` (tai khoan chinh SePay), roi chi cong tien cho ma nap khop duy nhat. Giao dich vao VA van thuoc tai khoan chinh trong API v1.

Admin bam **Doi soat SePay** trong menu quan tri hoac go `/reconcile` de chay ngay va xem ket qua. Neu Token loi, API qua tai, du lieu bat thuong hoac mot lan tra ve du 5000 giao dich, bot khong tien cursor va bao admin; nhung giao dich da ghi nhan van duoc chong trung khi thu lai. Khi chua co Token, doi soat dinh ky chua chay va webhook van hoat dong binh thuong.

Admin bam **Trang thai shop** hoac go `/status` trong chat rieng de xem webhook noi bo co dang chay, lan webhook SePay xu ly gan nhat, lan doi soat thanh cong, ban sao luu da kiem tra gan nhat, so giao dich chua khop va cac san pham con 0-2 tai khoan. Moc webhook chi bat dau duoc luu tu phien ban co `/status`; khong co giao dich moi thi khong co webhook moi, nen moc cu khong tu chung minh duong truyen bi loi. Lan doi soat cach hon 10 phut hoac ban sao luu cach hon 36 gio se duoc danh dau can kiem tra. Gio hien thi la GMT+7. Trang thai webhook noi bo khong kiem tra duong ngrok cong khai; cua so giam sat lam viec do.

## Quan ly shop trong chat rieng voi bot

Admin can nhan `/start` cho bot truoc khi nhan thong bao giao dich. Go `/admin` trong chat rieng de mo menu quan tri. Cac lenh admin chi hoat dong voi ID trong `ADMIN_IDS` va trong chat rieng.

De gui thong bao cho toan bo khach da dung bot, mo `/admin` va bam **Gui thong bao**, hoac go `/broadcast Noi dung can gui`. Bot hien ban xem truoc va so khach; chi bat dau gui sau khi admin bam **Gui cho tat ca**. Admin khong nam trong danh sach nhan. Bot gui lan luot, thu lai loi tam thoi va tiep tuc sau khi khoi dong lai. Khach da chan bot se duoc tinh la khong gui duoc. Dung `/broadcaststatus` hoac `/broadcaststatus ID` de xem tien do. `/cancel` huy thao tac truoc khi xac nhan.

```text
/admin
/broadcast Noi dung can gui
/broadcaststatus
/users
/audit
/user 123456789
/banned
/unban 123456789
/sales
/topups
/transactions
/products
/productinfo 2
/postproduct #2
/requireemail #2 on
/requireemail #2 off
/orderemail 12
/emaildone 12
/pendingemails
/emailremind
/emailremind 45
/emailremind 0
/addproduct Tai khoan dich vu A | 49000 | Han su dung 30 ngay
/setproductid #2 1
/moveproduct #2 1
/editproduct #2 | Ten moi | Mo ta moi
/setprice #2 59k
/sale #2 19k 40
/endsale #2
/hide #2
/show #2
/deleteproduct #2
/archived
/restoreproduct #2
/stock #2 email@example.com | mat-khau-123
/bulkstock #2
/inventory #2
/stockitem 3
/warranties
/warranties all
/warrantyremind
/warrantyremind 60
/warrantyremind 0
/resolvewarranty 1 | Da cap tai khoan thay the, vui long thu lai
/report
/report 2026-09-26
/funnel 7
/funnel 30
/export
/export 2026-09-01 2026-09-30
/unmatched
/manualcredits
/status
/reconcile
/addcoupon SALE10 | 10% | 100 | 2026-12-31
/addcoupon GIAM5K | 5000 | 20
/addcoupon RIENGSP2 | 10% | 20 | #2
/addcoupon RIENGSP2HAN | 5000 | 20 | 2026-12-31 | #2
/coupons
/disablecoupon SALE10
/enablecoupon SALE10
/addbundle Combo 2 loai | 80k | #1x1,#2x1 | Giao 2 tai khoan
/editbundle 1 | Ten moi | 75k | Mo ta moi
/adminbundles
/hidebundle 1
/showbundle 1
/deletebundle 1
/restorebundle 1
```

`/users` hien tong so Telegram ID khach da tung tuong tac voi bot o dau moi trang, khong tinh ID admin, roi liet ke ten Telegram (neu da ghi nhan), ID, so du, so don va hai san pham khach da mua gan nhat kem so luong. Bam vao mot nguoi dung (hoac go `/user ID`) de xem tong nap, tong mua, so luong tung san pham da mua va cac don gan day duoc gom theo lan thanh toan. Nut **Xem tat ca san pham da mua** hien du danh sach qua tung trang; nut **Xem don** mo chi tiet don do. Bot cap nhat ten khi khach gui `/start` hoac `/menu`; khach cu chua quay lai se duoc hien bang ID. `/sales` gom moi lan thanh toan thanh mot don, liet ke cac tai khoan ben trong. Moi don moi duoc thong bao mot lan cho cac ID admin, gom nguoi mua, cac san pham, ma don va tong tien; tin khong chua thong tin dang nhap cua tai khoan. Thong bao duoc luu hang doi trong SQLite va thu gui lai neu Telegram loi hoac bot khoi dong lai; don cu truoc khi bat tinh nang khong bi gui lai. `/topups` hien ma nap, so tien goi y va so tien da nhan; `/transactions` hien giao dich ngan hang va trang thai ghi co.

`/users` cung hien hai san pham khach mo chi tiet gan nhat neu co du lieu. Mo nguoi dung de xem so luot mo va thoi diem gan nhat cua tung san pham; bam **Xem tat ca san pham da xem** de xem danh sach phan trang. So luot nay tinh tu khi bat thong ke, gom moi ngay da ghi nhan; viec chi thay san pham trong danh muc khong duoc tinh.

Admin mo `/sales`, chon don da ban roi bam **Nhan khach**, hoac bam **Nhan khach** ngay tren thong bao don moi. Bot hoi noi dung va gui rieng cho nguoi mua cua don do. Khach bam **Tra loi chu shop** trong tin nhan de gui phan hoi qua bot; admin nhan phan hoi kem nut **Tra loi khach**. Moi luot nhap co hieu luc 10 phut, toi da 2000 don vi ky tu Telegram; `/cancel` huy luot nhap. Bot chi chuyen phan hoi khi chinh nguoi mua bam nut trong tin shop gui, khong chuyen moi tin khach gui vao bot. Neu khach da chan bot, admin duoc bao chua gui duoc.

Bot canh cao khi mot khach thao tac qua nhanh vuot gioi han rieng; moi canh cao cach nhau it nhat 30 giay. Neu nhan 3 canh cao trong 1 gio gan nhat, khach bi cam su dung bot cho den khi admin bo cam. Admin cung co the mo `/admin` > **Nguoi dung** > chon khach > **Ban nguoi dung** > **Xac nhan ban** de cam thu cong; khong the ban tai khoan admin. Danh sach `/admin` > **Nguoi dung bi cam** (hoac `/banned`) hien ly do spam hay admin. Bam **Bo cam** trong chi tiet khach/danh sach cam, hoac go `/unban Telegram_ID`, de cho khach dung lai. Canh cao va lenh cam duoc luu trong SQLite nen van con sau khi khoi dong lai. Gioi han chung khi nhieu nguoi dung dong thoi khong tinh la canh cao ca nhan; admin khong bi canh cao. Khach bi cam khong nhan tin quang ba moi; so du, don hang va giao dich nap da co van duoc giu nguyen. Giao dich dang cho thanh toan van duoc xu ly neu ngan hang ghi nhan tien. Truong hop khach tu chan bot trong Telegram la trang thai khac.

Admin mo `/admin` > **Nhat ky quan tri** (hoac go `/audit`) de xem ID admin, thoi gian GMT+7, thao tac, doi tuong va chi tiet ngan. Nhat ky ghi ca thao tac tu nut bam va lenh admin, vi du ban/bo cam user, cong tien thu nghiem, xu ly email/bao hanh, thay doi san pham va gui thong bao. Bot khong ghi mat khau, ma OTP, noi dung tai khoan kho hay noi dung broadcast vao nhat ky. Bam **Xuat nhat ky CSV** de luu file rieng cho doi soat.

Neu giao dich tien vao chua khop ma nap, admin mo `/unmatched` trong chat rieng va bam **Xu ly #ID**. Doi chieu giao dich voi SePay/ngan hang va xac minh khach truoc khi nhap ma `NAP...` do bot da cap cho khach. Bot hien lai ID khach, ma nap va **so tien thuc nhan**; chi bam **Xac nhan cong tien** khi tat ca dung. Nut xac nhan het han sau 5 phut; `/cancel` hoac nut **Huy** bo thao tac. Moi giao dich chi duoc cong mot lan, ke ca khi webhook/doi soat gui lai. `/manualcredits` cho biet giao dich nao da xu ly tay, nguoi nhan, so tien, admin xu ly va thoi gian. Neu khong xac minh duoc nguoi chuyen, de giao dich o trang thai chua khop; bot khong tu chung minh duoc giao dich do thuoc khach nao.

`/shop` xep san pham theo so hien thi trong mot cot nut bam, moi nut hien ten, gia hien tai va so tai khoan con lai. Tin nhan chi hien tieu de va huong dan, khong lap lai danh sach san pham o phan chu. Khi co hon 60 san pham, bot hien nut chuyen trang. So da ban van hien trong chi tiet san pham neu da co don thuc te; so nay tinh ca tai khoan giao trong goi. `/products` cho phep admin mo tung san pham va xem so tai khoan chua ban/da ban, ke ca khi bang 0. Bam **Xem kho** trong san pham hoac go `/inventory #ID` de thay ngay thong tin tai khoan chua ban (toi da 8 ma kho/trang), khong can bam tung ma. Bam **Xem tai khoan da ban** de xem danh sach da giao va bam tung tai khoan de xem noi dung, don hang, khach mua, gia va thoi gian. Don trong muc **Don da ban** cung co nut xem tai khoan da giao. Tin chua tai khoan chi hien trong chat rieng cua admin, duoc bao ve noi dung va tat xem truoc lien ket; neu qua dai, bot chia thanh nhieu tin. `/stockitem ID` van xem duoc mot tai khoan chua ban cu the. Moi lenh `/stock` them mot tai khoan; bot khong ban lap lai cung thong tin tai khoan.

Nut **Xem san pham** trong menu co mau xanh. Trong `/shop` va `/bundles`, nut san pham/goi con hang mau xanh, het hang mau do; nut **Mua ngay** mau xanh. Mau cu the do ung dung Telegram cua khach hien thi, va tin nhan cu can mo lai `/menu` hoac `/shop` de thay nut moi.

Quan ly san pham khong can go cu phap lenh: admin mo `/admin` > **San pham** > **Them san pham** hoac chon mot san pham trong danh sach. Cac nut trong chi tiet cho phep sua ten, mo ta, gia, nhap tai khoan vao kho, tao/ket thuc sale, bat/tat yeu cau email, hien/an, doi so hien thi, sap xep, tao bai dang va luu tru. Bot hoi gia tri can nhap tung buoc; tao san pham, tao sale va luu tru co buoc xac nhan. **San pham da luu tru** co nut khoi phuc va se tro ve trang thai an. Nut **Huy** bo thao tac dang nhap. Cac lenh cu van hoat dong neu admin muon dung.

Admin mo `/admin` > **San pham** > chon mot san pham, bam **Doi so hien thi** roi gui so moi (1–999999). Danh sach san pham khach thay trong `/shop` va **San pham theo doi** luon xep theo so `#` tang dan, ke ca khi chuyen trang. Neu so moi da co, bot hoan doi so hien thi cua hai san pham, ke ca san pham da luu tru. Co the go `/setproductid #SO_HIEN_TAI SO_MOI`. Nut **Len/Xuong** trong chi tiet va `/moveproduct #ID VI_TRI` chi sap xep danh sach **quan tri** (vi tri bat dau tu 1); khong doi thu tu khach thay. Moi lenh quan ly san pham dung so `#` hien tai, vi du `/setprice #1 10k`, `/bulkstock #1`, `/addcoupon CODE | 10% | 20 | #1`. Neu nhap so khong co `#` va so do cung la ma noi bo cua san pham khac, bot se tu choi va huong dan nhap `#`; khong sua nham san pham. ID noi bo van duoc giu cho don hang, kho, nut mua va link da chia se. Tin danh sach cu trong Telegram khong tu cap nhat; mo lai `/products` de xem so moi. San pham da luu tru khong xuat hien trong danh sach khach cho den khi duoc khoi phuc va hien lai.

Khi admin dung `/setprice #ID GIA_MOI` de ha gia thuong, bot luu gia ngay truoc do va hien gia cu gach ngang, gia moi ben canh trong danh sach, chi tiet va san pham theo doi. Neu tang gia, bot chi hien gia moi; nhap lai dung gia hien tai thi giu nguyen cach hien thi. Gia sale dang chay uu tien hien gia thuong gach ngang va gia sale; phai dung `/endsale #ID` truoc khi doi gia thuong. `/editbundle` ap dung cung quy tac khi doi gia goi. Bai dang san pham do bot tao hien gia cu gach ngang trong tin xem truoc; nut chia se qua Telegram gui van ban "Gia cu/Gia moi" vi duong dan chia se khong giu dinh dang chu. Tin da gui truoc do khong tu sua, va thanh toan van kiem tra gia hien tai truoc khi tru tien.

San pham mac dinh **khong yeu cau email**. Admin mo `/products` > chon san pham > **Yeu cau email**, hoac go `/requireemail #ID on`; dung `/requireemail #ID off` de tat. Khi khach bam mua san pham can email, bot hoi email truoc buoc xac nhan va thu xoa tin nhan khach vua nhap. Khach co the nhap mot email hoac nhieu email cach nhau bang dau phay, vi du `a@example.com, b@example.com` (toi da 20 email va 2000 ky tu); moi dia chi deu duoc kiem tra. **Moi email tinh mot san pham**: 2 email can 2 muc kho, thanh toan gia cua 2 san pham va tao mot don co 2 muc tai khoan, moi muc gan voi mot email. Neu thieu kho, thieu so du hoac gia thay doi truoc luc xac nhan thi khong giao hang va khong tru tien mot phan. Email sai phai nhap lai; o buoc xac nhan co nut **Sua email**; `/cancel` huy thao tac. Email cua tung muc tai khoan duoc luu ma hoa va khach xem lai trong `/orders`. Neu mua goi chua san pham can email, danh sach email van gan vao cac muc tai khoan can email cua **mot goi**, va gia goi tinh mot lan. Bot kiem tra lai yeu cau email ngay luc tru tien, nen bat yeu cau email giua luc khach xem hang va xac nhan se khong tao don thieu email.

Admin mo **Don da ban** > **Don #ID** > chon muc tai khoan, hoac go `/orderemail ID_tai_khoan` trong chat rieng de xem email; thong bao don moi chi co nut mo don, khong hien email. Don cu va san pham khong yeu cau email se khong co email. Email khong co trong bao cao CSV va khong duoc dung de tu dong gui tai khoan qua email; tai khoan van duoc giao trong bot. Neu bot khong xoa duoc tin nhan email khach da nhap, bot nhac khach tu xoa tin do.

Don co email hien trang thai **cho xu ly** cho den khi admin da thuc su xu ly xong. Admin mo **Don da ban** > **Don #ID** > chon muc tai khoan > **Da xu ly xong · Bao khach**, hoac go `/emaildone ID_tai_khoan`. Bot danh dau rieng tung muc tai khoan: neu khach nhap hai email khi mua mot san pham, se co mot don voi hai muc va can danh dau hai lan. Moi muc chi duoc danh dau mot lan; bam lai khong xep them thong bao. Bot gui thong bao hoan tat trong chat Telegram cua dung khach, kem nut mo don, khong gui email ra ngoai va khong lo dia chi email hay thong tin dang nhap trong tin. Neu Telegram tam loi, bot thu lai sau khi khoi dong; neu khach da chan bot, trang thai thong bao la **khong gui duoc** trong chi tiet don. Khach xem trang thai xu ly email qua `/orders`. Don khong yeu cau email khong co nut hoan tat nay.

Admin mo `/admin` > **Don email cho** hoac go `/pendingemails` de xem cac don can xu ly, xep don cu nhat len truoc. Mac dinh bot gui mot tin tong hop cho tung admin khi co don email cho qua **30 phut**. Admin bam **Nhac don email** hoac go `/emailremind` de xem va chon muc 15/30/60/120 phut; go `/emailremind SO_PHUT` de dat 5–1440 phut, hoac `/emailremind 0` de tat. Khi van con don qua han, bot nhac lai sau it nhat 60 phut (hoac sau thoi gian da dat neu dai hon). Don moi qua han co the duoc bao som hon nhung cac tin nhac cach nhau it nhat 15 phut. Lich nhac luu trong SQLite qua cac lan khoi dong lai; Telegram loi tam thoi thi bot thu lai. Tin nhac chi co so don va thoi diem don cu nhat, khong chua email hay tai khoan trong kho. Danh dau `/emaildone ID` se dua don khoi danh sach cho xu ly.

Khach mo chi tiet san pham hoac go dang con hang se thay nut **Chia se**. Nut mo hop chon nguoi nhan cua Telegram voi link den dung san pham/goi; nguoi nhan bam link se mo bot va xem gia, sale, ton kho hien tai cung nut mua. Admin cung co the vao `/shop` hoac `/bundles` de chia se link khi dang bai ban hang. San pham/goi da an hoac het hang khong hien nut chia se; link cu van mo duoc nhung khong cho mua neu hang khong con ban. Link khong chua thong tin dang nhap tai khoan.

De tao bai dang san pham, admin mo `/admin` > **San pham** > chon san pham > **Tao bai dang**, hoac go `/postproduct #ID` (so hien tai trong /products). Bot gui rieng cho admin mot bai viet san gom ten, mo ta, gia hien tai (neu dang sale thi hien ca gia goc va gia sale), ghi chu can email neu co, va link mo thang san pham. Bam **Chia se vao nhom** de chon nhom trong Telegram, hoac sao chep/chuyen tiep tin bai dang. Chuc nang chi dung cho san pham dang ban va con hang; khong tu gui cho khach hay dang len nhom. Gia va ton kho trong bai dang la anh chup tai luc tao, con link luon mo chi tiet moi nhat. Bai dang khong chua thong tin tai khoan trong kho.

Khach mo chi tiet san pham (ke ca dang het hang) va bam **🔔 Bao khi co hang/giam gia** de theo doi. Mo `/menu` > **San pham theo doi** hoac go `/following` de xem va bo theo doi. Moi san pham chi can theo doi mot lan; lua chon duoc luu qua cac lan khoi dong lai. Bot chi gui tin nhap kho va sale cho khach da theo doi **dung san pham do** truoc khi admin tao dot thong bao. Bam **Bo theo doi** trong chi tiet/danh sach/tin thong bao se huy cac tin cua san pham do con dang cho gui. San pham an/luu tru khong nhan nguoi theo doi moi; khach cu van co the bo theo doi. Cac tin hang moi/sale cho toan bo khach con trong hang doi tu phien ban cu se duoc bo qua khi nang cap, tranh gui ngoai y muon. Tin admin soan qua `/broadcast` van gui theo co che rieng.

Admin mo `/admin` > **San pham** > chon san pham > **Khach theo doi** de xem tong so khach va danh sach ID, ten Telegram, thoi diem bat dau theo doi. Danh sach chi tinh nguoi van dang theo doi, khong tinh admin; khach tat thong bao kho van hien trong danh sach vi ho van nhan tin sale. Bam ID de mo chi tiet khach. Danh sach nay chi admin xem duoc.

`/addproduct` chi tao san pham, chua thong bao cho khach. Moi lan admin nhap kho bang `/bulkstock #ID`, bot gui tin **🆕 HÀNG MỚI VỀ!** gom ten san pham, so tai khoan vua them, ton kho hien tai, gia va nut **🛒 Mua ngay** cho khach dang theo doi san pham va bat thong bao kho; `/stock #ID` khong gui thong bao. Mo lenh `/bulkstock` nhung chua gui tai khoan, huy lenh, hoac lo khong hop le deu khong thong bao. Bot gui lan luot de tranh gioi han Telegram, luu hang doi trong SQLite de tiep tuc neu bot khoi dong lai, va tu thu lai loi mang. San pham dang an, da het hang va khach da chan bot se khong nhan duoc tin.

Moi tin hang moi co nut **🔕 Bo theo doi san pham** de dung ca tin kho lan sale rieng san pham do. Khach cung co the tat toan bo **tin nhap kho** cua cac san pham dang theo doi tai `/menu` > **Thong bao kho** hoac `/stocknotify`; cac tin kho dang cho gui se duoc huy. Bat lai chi ap dung cho cac dot nhap kho moi; tin sale cua san pham dang theo doi va cac tin quan trong khac van hoat dong.

`/bulkstock #ID` cho phep dan nhieu tai khoan: moi dong la mot tai khoan, toi da 100 dong/lua. Co the gui cac dong trong cung tin nhan sau lenh, hoac gui o tin nhan tiep theo sau khi bot hoi. Bot nhap **ca tai khoan trung** trong lo hoac da tung co trong kho (ke ca da ban/o san pham khac), roi bao so dong trung sau khi xu ly; so dong duoc tinh theo cac dong co noi dung. Tai khoan trung co the duoc giao cho nhieu khach. `/stock #ID` van bao loi neu tai khoan trung. Bot co gang xoa tin nhan chua tai khoan; neu xoa that bai, hay tu xoa tin nhan do. Khi mua hang lam kho con 2 tai khoan tro xuong, bot bao admin mot lan; nhap them len tren 2 tai khoan se bat lai canh bao cho lan sau.

Neu dan tai khoan tu bang Markdown co dang `| email|matkhau|`, bot bo dau `| ` dau dong va dau `|` dong bang truoc khi luu; dau `|` giua cac truong duoc giu nguyen.

Khach mo `/orders`, chon don va bam **Yeu cau bao hanh don**, sau do gui mot noi dung cho ca don. Moi don chi co mot yeu cau dang mo, du trong don co nhieu tai khoan. Nut cu gan voi mot tai khoan van mo dung don chung. Admin xem `/warranties` (hoac `/warranties all` de xem lich su), chon yeu cau va phan hoi bang `/resolvewarranty ID | Noi dung`. Phan hoi duoc luu trong don hang va bot gui thong bao cho khach. Noi dung bao hanh va phan hoi duoc ma hoa trong SQLite; khong gui ma OTP hoac mat khau khac qua yeu cau.

Danh sach `/warranties` xep yeu cau dang mo cu nhat len truoc va hien thoi gian da cho. Mac dinh, bot nhac tung admin khi co yeu cau bao hanh chua xu ly qua **60 phut**. Mo `/admin` > **Nhac bao hanh** hoac go `/warrantyremind` de chon 15/30/60/120 phut; `/warrantyremind SO_PHUT` dat 5–1440 phut, `/warrantyremind 0` de tat. Neu van con yeu cau qua han, bot nhac lai sau it nhat 60 phut (hoac sau thoi gian da dat neu dai hon); yeu cau moi qua han co the duoc bao som hon nhung hai tin cach nhau it nhat 15 phut. Moi tin chi co so yeu cau va thoi diem cu nhat, kem nut mo danh sach; khong chua noi dung bao hanh hay tai khoan. Trang thai nhac duoc luu trong SQLite qua cac lan khoi dong lai; loi Telegram tam thoi se duoc thu lai. Xu ly xong bang `/resolvewarranty ID | Noi dung` se dua yeu cau khoi nhom can nhac.

`/report` cho thay 7 ngay gan nhat; `/report YYYY-MM-DD` cho mot ngay. Ngay duoc tinh theo gio Viet Nam (GMT+7). Tien nap la tong tien **thuc nhan va da ghi co** trong ngay; doanh thu va so don lay tu don mua thanh cong. So tien nap khong phai doanh thu.

Trong `/admin`, bam **Hanh trinh khach** hoac go `/funnel 7`, `/funnel 30` de xem so khach duy nhat da tuong tac, xem san pham/goi, tao ma nap, nap thanh cong va mua hang trong 7/30 ngay gan nhat (GMT+7). Bao cao loai admin; moi khach chi tinh mot lan o moi muc. Ty le "Xem → tao ma nap" va "Xem → mua" chi tinh hanh dong sau lan xem dau tien trong ky. Cac muc nap/mua co the khong theo thu tu neu khach da co so du tu truoc. Luot vao va xem chi bat dau ghi tu khi tinh nang nay duoc bat; tien nap va don lay tu du lieu giao dich that trong cung khoang theo doi.

Trong `/admin`, bam **San pham khach xem** de xem san pham nao duoc mo nhieu nhat trong 7 hoac 30 ngay: tong luot mo va so khach rieng biet. Danh sach chi hien san pham co luot xem, toi da 20 san pham moi trang de shop nho nam trong mot tin nhan. Bam vao san pham de xem khach nao da xem, moi nguoi mo bao nhieu lan va lan gan nhat. Bot chi ghi khi khach mo trang chi tiet san pham, ke ca qua link chia se; admin va lan bot tu hien lai trang sau khi bam theo doi khong duoc tinh. Thong ke bat dau tu khi cap nhat tinh nang nay, khong co du lieu lich su truoc do.

Admin bam **Xuat bao cao CSV** hoac go `/export` roi gui hai ngay `YYYY-MM-DD YYYY-MM-DD`; co the go ngay `/export 2026-09-01 2026-09-30`. Khoang ngay tinh theo GMT+7, gom ca ngay bat dau va ket thuc, toi da 90 ngay. Bot gui mot file ZIP gom `topups.csv` (tien da ghi co thuc te), `orders.csv` (moi tai khoan da ban mot dong, co purchase_id cua don chung), `manual_credits.csv` (lich su admin xu ly tay) va `README.txt`. Lich su xu ly tay la mot phan cua tien nap, **khong cong lai** vao tong tien nap. CSV dung UTF-8 BOM de mo trong Excel; khong chua mat khau tai khoan trong kho nhung co ID va ten khach, nen giu file rieng tu. Neu bao cao qua 50.000 dong hoac 15 MB, chia nho khoang ngay de xuat.

Trong `/admin` > **Nguoi dung**, bam **Xuat file nguoi dung** de nhan CSV cua tat ca khach tung truy cap (khong gom admin): Telegram ID, username, ten, ngay vao bot, so du, tien nap that, tien thu nghiem, tien da mua va so don. Trong `/admin` > **Don da ban**, bam **Xuat file san pham da ban** de nhan CSV toan bo lich su ban: moi don va moi san pham mot dong, co khach mua, so luong, don gia neu dong nhat, tong tien va thoi gian GMT+7. File khong chua mat khau, email hay noi dung giao hang. Hai file mo duoc bang Excel tren Windows va chi gui trong chat rieng cho admin; giu file rieng tu vi co thong tin khach. Moi file toi da 50.000 dong va 15 MB.

`/deleteproduct` luu tru san pham, khong xoa don hang hay tai khoan trong kho. `/restoreproduct` khoi phuc o trang thai an; dung `/show` khi san sang ban lai. Ten san pham tren don da mua duoc giu theo thoi diem mua. Khach nhan `/start` hoac `/menu` de mo menu nut bam. San pham chua co tai khoan trong kho duoc hien la tam het hang va khong co nut mua.

## Ma giam gia va goi san pham

Admin mo `/admin` > **Ma giam gia** > **Tao ma giam gia**. Bot hoi ma, muc giam, so luot; sau do bam nut chon **Toan shop** hoac mot san pham, chon han dung va xac nhan. Bam vao mot ma trong danh sach de sua muc giam, so luot, ngay het han hoac bat/tat. Ma va pham vi khong doi sau khi tao; so luot toi da khong the thap hon so lan da dung. Mo `/admin` > **Goi san pham** > **Tao goi**, gui ten, bam nut chon san pham va so luong, gui gia va mo ta, roi xac nhan. Bam vao goi de sua ten, gia, mo ta, an/hien ban, luu tru hoac khoi phuc. Thanh phan goi khong sua sau khi tao; neu can doi, luu tru goi cu va tao goi moi. Nut **Huy** hoac `/cancel` bo thao tac dang nhap. Cac lenh cu van dung duoc.

Khach chon san pham hoac mo `/bundles`, bam Mua, roi bam **Nhap ma giam gia** neu co ma. Ma duoc kiem tra lai khi bam Xac nhan mua. Ma khong gan ID san pham van ap dung toan shop cho san pham le va goi. De tao ma chi dung cho mot san pham, admin dung `/addcoupon CODE | 10% hoac 10000 | So_luot | ID_san_pham`; neu can ngay het han, dung `/addcoupon CODE | 10% | So_luot | YYYY-MM-DD | ID_san_pham`. Xem ID bang `/products`. Ma rieng san pham khong ap dung cho san pham khac hoac goi. Moi ma co gioi han tong so luot, moi khach dung toi da 1 lan; khong tu dong cong don nhieu ma. Ngay het han (neu dat) duoc tinh den het ngay GMT+7. Gia sau giam phai con duong, va voi goi phai con it nhat 1d cho moi tai khoan de ghi don. Admin xem pham vi ma va tat/bat bang `/coupons`, `/disablecoupon CODE`, `/enablecoupon CODE`.

Admin dung `/sale ID Gia_sale So_suat` de bat sale cho mot san pham dang ban va co san kho, vi du `/sale 2 19k 40`. Gia sale phai thap hon gia goc, so suat khong vuot qua ton kho luc bat. Bot tu xep tin **🔥 SALE SỐC!** cho khach dang theo doi san pham do (tru admin), gom ten san pham, gia goc gach ngang, gia sale va phan tram giam, so suat, ton kho va nut **🛒 Mua ngay**. Tin duoc gui lan luot, thu lai khi loi mang va tiep tuc sau khi bot khoi dong lai. Gia sale tu ap dung cho cac don mua le dau tien den khi het suat; bot kiem tra lai gia va suat trong giao dich SQLite luc xac nhan de khong ban vuot gioi han. Khi het suat, gia tro ve gia goc va cac tin chua gui bi huy. `/endsale ID` dung sale som; an hoac luu tru san pham cung ket thuc sale. Can ket thuc sale truoc khi dung `/setprice` hoac tao sale khac. Ma giam gia khong dung dong thoi voi sale; gia goi san pham khong thay doi. `/setprice` khong tu tao sale hay gui tin sale.

`/addbundle Ten | Gia | IDxSL,IDxSL | Mo ta` tao goi gom 2-10 tai khoan tu kho san pham hien co. Vi du `1x2,2x1` giao 2 tai khoan san pham #1 va 1 tai khoan san pham #2. Gia goi luc tao phai thap hon tong gia le. Neu mot san pham bi an, luu tru hoac het kho, goi tam khong mua duoc. `/editbundle` sua ten/gia/mo ta; de doi thanh phan, luu tru goi cu va tao goi moi. Giao dich goi tru tien mot lan; khach nhan mot don chung gom cac tai khoan va mot bao hanh chung cho ca don. Bao cao tinh ca goi la mot lan mua va doanh thu theo so tien thuc thu. Gia tren tung muc tai khoan duoc chia tu gia goi.

## Kiem tra

```powershell
python -m unittest discover -s tests -v
```
