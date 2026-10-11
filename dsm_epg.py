import requests
import gzip
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# 输出文件
xml_path = "all.xml"
gz_path = "all.xml.gz"

# 频道列表
channels = [
    {"name": "CCTV-1综合", "id": "cctv1"},
    {"name": "CCTV-2财经", "id": "cctv2"},
    {"name": "CCTV-3综艺", "id": "cctv3"},
]

root = ET.Element("tv")
root.set("generator-info", "epg auto script")

for ch in channels:
    chan = ET.SubElement(root, "channel")
    chan.set("id", ch["id"])
    ET.SubElement(chan, "display-name").text = ch["name"]
    # 这里可以抓取节目，示例占位，替换成你原来的抓取逻辑
    programme = ET.SubElement(root, "programme")
    programme.set("channel", ch["id"])
    start_time = datetime.now().strftime("%Y%m%d%H%M00")
    stop_time = (datetime.now() + timedelta(hours=2)).strftime("%Y%m%d%H%M00")
    programme.set("start", start_time)
    programme.set("stop", stop_time)
    ET.SubElement(programme, "title").text = f"{ch['name']}节目"

tree = ET.ElementTree(root)
tree.write(xml_path, encoding="utf-8", xml_declaration=True)

# 压缩gz
with open(xml_path, "rb") as f_in:
    with gzip.open(gz_path, "wb") as f_out:
        f_out.writelines(f_in)

print(f"✅ 生成 {xml_path} , {gz_path}")
