from DrissionPage import ChromiumOptions, Chromium
from DrissionPage.common import Settings

# Settings._debug = True
b = Chromium()
b.set.auto_handle_alert()
t = b.latest_tab
# t.get(r"D:\coding\projects\DrissionPage\testing\content1.html")
for _ in range(10):
    t2 = t('#open').click.for_new_tab()
    print(t2.title)
    t2.close()
