#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging
import time
from logging.handlers import RotatingFileHandler
from .utils import get_app_subdir

import openai

def setup_task_logger(task_id):
    """
    为特定任务设置日志记录器
    
    Args:
        task_id: 任务ID
        
    Returns:
        logger: 配置好的日志记录器
    """
    log_dir = get_app_subdir('logs')
    os.makedirs(log_dir, exist_ok=True)
    
    log_file = os.path.join(log_dir, f'task_{task_id}.log')
    logger = logging.getLogger(f'ai_enhancer_{task_id}')
    
    if not logger.handlers:  # 避免重复添加处理器
        logger.setLevel(logging.INFO)
        
        # 文件处理器
        file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=5, encoding='utf-8')
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        
        # 确保消息不会传播到根日志记录器
        logger.propagate = False
    
    return logger

def get_openai_client(openai_config):
    """
    创建OpenAI客户端 (兼容 API 1.x 版本)
    
    Args:
        openai_config (dict): OpenAI配置信息，包含api_key, base_url等
        
    Returns:
        OpenAI客户端实例
    """
    # 配置选项
    api_key = openai_config.get('OPENAI_API_KEY', '')
    options = {}
    
    # 如果提供了base_url，添加到选项中
    if openai_config.get('OPENAI_BASE_URL'):
        options['base_url'] = openai_config.get('OPENAI_BASE_URL')
    
    # 创建并返回新版客户端实例
    return openai.OpenAI(api_key=api_key, **options)

def translate_text(text, target_language="zh-CN", openai_config=None, task_id=None):
    """
    使用OpenAI翻译文本
    
    Args:
        text (str): 待翻译的文本
        target_language (str): 目标语言代码，默认为简体中文
        openai_config (dict): OpenAI配置信息，包含api_key, base_url, model_name等
        task_id (str, optional): 任务ID，用于日志记录
        
    Returns:
        str or None: 翻译后的文本，出错时返回None
    """
    if not text or not text.strip():
        return text
    
    logger = setup_task_logger(task_id or "unknown")
    logger.info(f"开始翻译文本，目标语言: {target_language}")
    # 仅日志中显示部分内容，实际翻译用完整文本
    logger.info(f"原始文本 (截取前100字符用于显示): {text[:100]}...")
    logger.info(f"原始文本总长度: {len(text)} 字符")
    
    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.error("缺少OpenAI配置或API密钥")
        return None
    
    try:
        # 获取OpenAI客户端 (1.x版本)
        client = get_openai_client(openai_config)
        model_name = openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
        
        language_map = {
            'zh-CN': '简体中文',
            'zh-TW': '繁体中文',
            'en': '英语',
            'ja': '日语',
            'ko': '韩语',
            'es': '西班牙语',
            'fr': '法语',
            'de': '德语',
            'ru': '俄语'
        }
        
        target_language_name = language_map.get(target_language, target_language)
        
        # 构建翻译提示 - 简洁有效，重点在模仿原文表达风格
        prompt = f"""请将以下内容翻译成{target_language_name}，注意模仿原文的语言风格和表达方式：

**翻译原则：**
• 保持原文的语气、风格和表达习惯
• 如果原文口语化，译文也要口语化；如果原文正式，译文也要正式
• 保留原文的情感色彩和个人化表达
• 使用自然流畅的{target_language_name}，但不要过度本土化
• 直接输出翻译结果，不要添加说明或注释

**需要过滤的内容（直接忽略，不留痕迹）：**
• 所有网址链接和邮箱地址
• 社交媒体账号(@用户名、#标签)  
• "订阅"、"关注"、"点赞"等平台互动提示
• 广告推广和商业链接

原文:
{text}
"""
        
        start_time = time.time()
        
        # 使用新版API调用格式
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "你是一个专业翻译工具。你的工作是提供准确、规范的翻译，使用正式的书面语言风格，避免过度口语化和网络流行语。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=4096
        )
        
        response_time = time.time() - start_time
        
        # 新版API响应格式
        translated_text = response.choices[0].message.content.strip()
        
        # 检查并移除可能的前缀和注释
        prefixes_to_remove = ["翻译：", "译文：", "这是翻译：", "以下是译文：", "以下是我的翻译："]
        for prefix in prefixes_to_remove:
            if translated_text.startswith(prefix):
                translated_text = translated_text[len(prefix):].strip()
        
        # 移除各种形式的说明性文字
        import re
        removal_patterns = [
            r'（注：.*?）',                     # 中文括号注释
            r'\(注：.*?\)',                     # 英文括号注释
            r'【注：.*?】',                     # 方括号注释
            r'（.*?已移除）',                   # 各种"已移除"说明
            r'\(.*?已移除\)',                   # 英文括号的已移除说明
            r'（.*?联系方式.*?）',              # 联系方式相关说明
            r'\(.*?联系方式.*?\)',              # 英文括号联系方式说明
            r'（.*?社交媒体.*?）',              # 社交媒体相关说明
            r'\(.*?社交媒体.*?\)',              # 英文括号社交媒体说明
            r'（.*?标签.*?）',                  # 标签相关说明
            r'\(.*?标签.*?\)',                  # 英文括号标签说明
            r'（.*?链接.*?）',                  # 链接相关说明
            r'\(.*?链接.*?\)',                  # 英文括号链接说明
            r'（.*?推广.*?）',                  # 推广相关说明
            r'\(.*?推广.*?\)',                  # 英文括号推广说明
            r'（.*?广告.*?）',                  # 广告相关说明
            r'\(.*?广告.*?\)',                  # 英文括号广告说明
            r'（.*?removed.*?）',               # 英文removed说明
            r'\(.*?removed.*?\)',               # 英文括号removed说明
            r'（.*?filtered.*?）',              # 英文filtered说明
            r'\(.*?filtered.*?\)',              # 英文括号filtered说明
        ]
        
        for pattern in removal_patterns:
            translated_text = re.sub(pattern, '', translated_text, flags=re.IGNORECASE)
        
        logger.info(f"翻译完成，耗时: {response_time:.2f}秒")
        logger.info(f"翻译结果总长度: {len(translated_text)} 字符")
        logger.info(f"翻译结果 (截取前100字符用于显示): {translated_text[:100]}...")
        
        # 过滤URL、域名和邮箱地址
        import re
        
        # 更全面的URL正则表达式
        url_patterns = [
            r'https?://[^\s\u4e00-\u9fff]+',  # HTTP/HTTPS链接（排除中文字符）
            r'www\.[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',  # www开头的域名
            r'ftp://[^\s\u4e00-\u9fff]+',     # FTP链接
            r'\b[a-zA-Z0-9.-]+\.(?:com|net|org|edu|gov|mil|co|io|me|tv|fm|ly|be|to|cc|ws|biz|info|name|mobi|asia|tel|travel|museum|aero|jobs|cat|pro|xxx|app|dev|ai|tech|online|website|blog|shop|store|host|cloud|site|link|page|youtube|twitter|facebook|instagram|tiktok|linkedin|pinterest|tumblr|flickr|vimeo|twitch|discord|slack|telegram|whatsapp|reddit|github|gitlab|bitbucket|stackoverflow|medium|substack|patreon|ko-fi|paypal|venmo|cashapp|gofundme|kickstarter|indiegogo|etsy|amazon|ebay|aliexpress|shopify|wix|squarespace|wordpress|blogger|weebly|godaddy|namecheap|cloudflare|google|microsoft|apple|adobe|netflix|spotify|hulu|disney|hbo|paramount|peacock|crunchyroll|funimation|vrv|roosterteeth|newgrounds|deviantart|artstation|behance|dribbble|figma|sketch|canva|photoshop|zoom|teams|meet|skype|facetime|snapchat|clubhouse|onlyfans|cameo|twitch|mixer|dlive|caffeine|trovo|nimo|booyah|nonolive|bigo|uplive|liveme|periscope|younow|omegle|chatroulette|discord|teamspeak|mumble|ventrilo|curse|overwolf|steam|epic|origin|uplay|battlenet|gog|itch|humble|greenman|fanatical|chrono|bundlestars|indiegala|groupees|royalgames|gamersgate|direct2drive|impulse|desura|gamefly|onlive|gaikai|stadia|geforce|shadow|parsec|rainway|moonlight|steamlink|nvidia|amd|intel|corsair|razer|logitech|steelseries|hyperx|asus|msi|gigabyte|asrock|evga|zotac|sapphire|xfx|powercolor|his|club3d|gainward|palit|galax|kfa2|inno3d|pny|leadtek|point|view|manli|colorful|maxsun|yeston|mingying|onda|soyo|biostar|elite|foxconn|jetway|ecs|dfi|abit|chaintech|shuttle|via|sis|ali|uli|nvidia|ati|3dfx|matrox|s3|cirrus|tseng|oak|trident|chips|realtek|creative|aureal|ensoniq|yamaha|roland|korg|moog|oberheim|sequential|prophet|jupiter|juno|sh|tr|tb|mc|sp|mpc|mv|fantom|integra|rd|fp|go|bk|jv|xv|vsynth|variphrase|cosm|roli|arturia|novation|akai|native|ableton|steinberg|presonus|avid|digidesign|motu|rme|focusrite|scarlett|clarett|red|saffire|octopre|isa|liquid|voicemaster|twintrack|trackmaster|platinum|onyx|big|knob|studio|live|monitor|reference|truth|reveal|dynaudio|genelec|yamaha|kali|jbl|krk|mackie|behringer|tascam|zoom|roland|boss|tc|eventide|lexicon|alesis|akai|mpc|maschine|push|launchpad|oxygen|keystation|axiom|impulse|code|sl|mk|arturia|keystep|beatstep|drumbrute|microbrute|minibrute|matrixbrute|polybrute|pigments|analog|lab|collection|v|vintage|electric|stage|piano|clavinet|wurlitzer|rhodes|hammond|leslie|vox|continental|farfisa|acetone|combo|compact|drawbar|tonewheel|percussion|vibrato|chorus|reverb|tremolo|distortion|overdrive|fuzz|wah|phaser|flanger|delay|echo|compressor|limiter|gate|expander|eq|filter|synthesizer|sampler|sequencer|arpeggiator|vocoder|talkbox|autotune|melodyne|celemony|antares|waves|plugin|alliance|soundtoys|fabfilter|izotope|ozone|neutron|nectar|rx|insight|tonal|balance|music|rebalance|dialogue|match|de|noise|de|clip|de|click|de|crackle|de|hum|de|rustle|de|wind|spectral|repair|composite|view|advanced|healing|connect|portal|exponential|audio|cedar|sonnox|oxford|inflator|limiter|enhancer|eq|dynamics|reverb|transmod|fraunhofer|pro|codec|toolbox|surcode|dvd|dts|ac3|dolby|atmos|truehd|dtsma|pcm|flac|alac|aac|mp3|ogg|vorbis|opus|speex|gsm|amr|g711|g722|g726|g729|ilbc|silk|celt|wma|ra|rm|au|aiff|wav|bwf|rf64|caf|m4a|m4b|m4p|m4r|3gp|3g2|amv|asf|avi|drc|dv|f4v|flv|gif|gifv|m2v|m4v|mkv|mng|mov|mp2|mp4|mpe|mpeg|mpg|mpv|mxf|nsv|ogv|qt|rm|rmvb|roq|svi|vob|webm|wmv|yuv|divx|xvid|h264|h265|hevc|vp8|vp9|av1|theora|dirac|prores|dnxhd|cineform|blackmagic|raw|braw|r3d|arriraw|cinema|dng|exr|dpx|tiff|tga|bmp|jpg|jpeg|png|gif|webp|svg|eps|pdf|ps|ai|cdr|wmf|emf|cgm|dxf|dwg|step|iges|stl|obj|ply|x3d|collada|fbx|3ds|max|maya|blender|cinema4d|houdini|modo|lightwave|softimage|xsi|katana|nuke|fusion|resolve|premiere|avid|final|cut|pro|x|imovie|quicktime|vlc|media|player|classic|mpc|hc|be|potplayer|kmplayer|gom|player|smplayer|mpv|mplayer|xine|totem|banshee|rhythmbox|amarok|clementine|strawberry|foobar2000|winamp|musicbee|aimp|mediamonkey|jriver|plex|kodi|emby|jellyfin|serviio|universal|media|server|twonky|asset|upnp|dlna|chromecast|airplay|miracast|widi|intel|wireless|display|nvidia|shield|apple|tv|roku|fire|stick|android|tv|smart|tv|samsung|lg|sony|panasonic|philips|tcl|hisense|vizio|insignia|toshiba|sharp|jvc|mitsubishi|pioneer|onkyo|denon|marantz|yamaha|harman|kardon|bose|sonos|klipsch|polk|audio|definitive|technology|martin|logan|magnepan|wilson|audio|focal|kef|bowers|wilkins|paradigm|psa|svs|hsu|research|rythmik|audio|rel|acoustics|velodyne|jl|audio|rockford|fosgate|alpine|kenwood|pioneer|clarion|sony|jvc|panasonic|blaupunkt|continental|bosch|delphi|visteon|harman|becker|grundig|telefunken|nordmende|saba|loewe|metz|bang|olufsen|meridian|arcam|cambridge|audio|creek|cyrus|exposure|rega|naim|linn|chord|electronics|musical|fidelity|rotel|parasound|bryston|classe|audio|research|conrad|johnson|mcintosh|mark|levinson|krell|pass|labs|boulder|amplifiers|wilson|benesch|vandersteen|thiel|revel|infinity|jbl|synthesis|lexicon|proceed|madrigal|cello|spectral|mit|transparent|audioquest|kimber|kable|nordost|cardas|analysis|plus|purist|audio|design|siltech|crystal|cable|synergistic|research|shunyata|power|conditioning|ps|audio|furman|monster|panamax|tripp|lite|apc|cyberpower|eaton|liebert|emerson|network|power|vertiv|schneider|electric|legrand|wiremold|panduit|black|box|belkin|linksys|netgear|dlink|tplink|asus|cisco|juniper|hp|dell|lenovo|ibm|oracle|sun|microsystems|sgi|cray|fujitsu|nec|hitachi|toshiba|mitsubishi|panasonic|sharp|casio|citizen|seiko|epson|canon|nikon|sony|olympus|pentax|ricoh|fujifilm|kodak|polaroid|leica|hasselblad|mamiya|contax|bronica|rollei|voigtlander|zeiss|schneider|kreuznach|rodenstock|cooke|angenieux|fujinon|sigma|tamron|tokina|samyang|rokinon|bower|vivitar|quantaray|promaster|tiffen|hoya|b+w|heliopan|marumi|kenko|cokin|lee|filters|formatt|hitech|singh|ray|breakthrough|photography|polar|pro|nisi|haida|kase|wine|country|benro|gitzo|manfrotto|really|right|stuff|kirk|enterprises|arca|swiss|wimberley|jobu|design|promedia|gear|think|tank|photo|lowepro|billingham|ona|bags|peak|design|f|stop|gear|mindshift|tenba|domke|crumpler|kata|vanguard|gura|gear|pelican|storm|cases|b+h|adorama|amazon|best|buy|walmart|target|costco|sams|club|newegg|micro|center|fry|electronics|tiger|direct|provantage|cdw|insight|connection|zones|pc|mall|tech|data|systems|synnex|ingram|tech|arrow|electronics|avnet|mouser|digikey|newark|element14|rs|components|allied|electronics|future|electronics|quest|components|chip|one|stop|findchips|octopart|datasheets|alldatasheet|electronic|components|distributor|supplier|manufacturer|oem|odm|ems|pcb|assembly|smt|through|hole|bga|qfp|soic|ssop|tssop|msop|dfn|qfn|wlcsp|flip|chip|wire|bond|die|attach|encapsulation|molding|test|burn|programming|functional|boundary|scan|jtag|spi|i2c|uart|usb|ethernet|can|lin|flexray|most|lvds|mipi|csi|dsi|hdmi|displayport|thunderbolt|pcie|sata|sas|scsi|fc|infiniband|roce|iwarp|omnipath|nvlink|ccix|cxl|gen|z|ddr|gddr|hbm|lpddr|nand|nor|flash|eeprom|fram|mram|rram|pcm|3d|xpoint|optane|nvdimm|dimm|sodimm|udimm|rdimm|lrdimm|fbdimm|simm|sipp|dip|sip|zip|plcc|pga|bga|csp|sop|tsop|vsop|ssop|tssop|msop|soic|sol|qfp|lqfp|tqfp|pqfp|bqfp|cqfp|mqfp|sqfp|dfn|qfn|mlf|son|wson|uson|xson|dson|lfcsp|wlcsp|fcbga|pbga|cbga|ccga|lccc|plcc|clcc|cerquad|cerdip|pdip|sdip|shrink|dip|skinny|dip|zip|pga|cpga|fcpga|ppga|spga|bga|pbga|cbga|fcbga|tbga|fbga|lbga|mbga|sbga|ubga|csp|wlcsp|fcsp|lga|land|grid|array|socket|slot|connector|header|receptacle|plug|jack|terminal|block|barrier|strip|wire|to|board|board|to|board|cable|assembly|harness|ribbon|flat|flex|ffc|fpc|coaxial|twinax|triax|twisted|pair|shielded|unshielded|cat5|cat5e|cat6|cat6a|cat7|cat8|fiber|optic|single|mode|multi|mode|om1|om2|om3|om4|om5|os1|os2|lc|sc|st|fc|mtp|mpo|e2000|mu|mt|rj|din|xlr|bnc|sma|smb|smc|mmcx|mcx|u|fl|ipex|hirose|jst|molex|te|connectivity|tyco|amp|deutsch|itt|cannon|amphenol|souriau|glenair|radiall|rosenberger|huber|suhner|times|microwave|southwest|microwave|pasternack|fairview|microwave|mini|circuits|analog|devices|texas|instruments|infineon|stmicroelectronics|nxp|semiconductors|renesas|microchip|technology|maxim|integrated|linear|technology|analog|devices|intersil|idt|integrated|device|technology|cypress|semiconductor|lattice|semiconductor|microsemi|actel|altera|xilinx|intel|psg|programmable|solutions|group|amd|xilinx|zynq|ultrascale|kintex|virtex|artix|spartan|cyclone|arria|stratix|max|ecp5|machxo|crosslink|fpga|cpld|soc|mpsoc|rfsoc|acap|versal|zynq|mpsoc|ultrascale|plus|kintex|ultrascale|virtex|ultrascale|zynq|7000|series|spartan|6|cyclone|v|arria|10|stratix|10|agilex|7|series|ultrascale|plus|versal|acap|adaptive|compute|acceleration|platform|ai|engine|dsp|slice|block|ram|bram|uram|distributed|ram|shift|register|lut|lookup|table|carry|chain|dsp48|dsp58|multiplier|accumulator|mac|fir|filter|iir|filter|fft|dft|cordic|floating|point|fixed|point|arithmetic|logic|unit|alu|processor|core|arm|cortex|a|r|m|series|risc|v|mips|powerpc|x86|intel|atom|core|xeon|pentium|celeron|amd|ryzen|threadripper|epyc|athlon|a|series|fx|series|phenom|opteron|nvidia|geforce|rtx|gtx|titan|quadro|tesla|a100|v100|p100|k80|m40|m60|gtx|1080|1070|1060|1050|rtx|2080|2070|2060|rtx|3090|3080|3070|3060|rtx|4090|4080|4070|4060|radeon|rx|vega|navi|rdna|rdna2|rdna3|gcn|polaris|fiji|hawaii|tahiti|pitcairn|cape|verde|bonaire|oland|hainan|tonga|antigua|grenada|ellesmere|baffin|lexa|vega|10|vega|20|navi|10|navi|14|navi|21|navi|22|navi|23|navi|24|big|navi|small|navi|sienna|cichlid|navy|flounder|dimgrey|cavefish|beige|goby|yellow|carp|navi|31|navi|32|navi|33|plum|bonito|wheat|nas|raphael|rembrandt|barcelo|cezanne|renoir|picasso|raven|ridge|bristol|ridge|carrizo|kaveri|richland|trinity|llano|brazos|ontario|zacate|bobcat|jaguar|puma|excavator|steamroller|piledriver|bulldozer|k10|k8|k7|k6|k5|am4|am3|am2|fm2|fm1|s1|s1g4|s1g3|s1g2|s1g1|asb2|asa|tr4|trx40|sp3|sp4|sp5|lga|1151|1150|1155|1156|1366|2011|2066|3647|4189|bga|1440|1515|1356|1364|1168|956|827|479|478|423|370|socket|a|b|c|d|e|f|g|h|j|k|l|m|n|p|q|r|s|t|u|v|w|x|y|z|0|1|2|3|4|5|6|7|8|9)\b',  # 常见域名
        ]
        
        # 邮箱正则表达式
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        
        # 社交媒体账号引用
        social_patterns = [
            r'@[A-Za-z0-9_]+',     # @用户名
            r'#[A-Za-z0-9_]+',     # #标签
        ]
        
        # 平台互动提示词（中英文）
        interaction_patterns = [
            r'订阅[我们的]*[频道]*',
            r'关注[我们]*',
            r'点赞[这个]*[视频]*',
            r'分享[给]*[朋友们]*',
            r'评论[区]*[见]*',
            r'更多[内容]*请访问',
            r'详情见[链接]*',
            r'链接在[描述]*[中]*',
            r'访问[我们的]*[网站]*',
            r'查看[完整]*[版本]*',
            r'下载[链接]*',
            r'购买[链接]*',
            r'subscribe\s+to\s+[our\s]*channel',
            r'follow\s+[us\s]*',
            r'like\s+[this\s]*video',
            r'share\s+[with\s]*[friends\s]*',
            r'check\s+out\s+[our\s]*[website\s]*',
            r'visit\s+[our\s]*[site\s]*',
            r'download\s+[link\s]*',
            r'buy\s+[link\s]*',
            r'more\s+info\s+at',
            r'see\s+[full\s]*[version\s]*',
        ]
        
        # 应用所有URL模式
        for pattern in url_patterns:
            translated_text = re.sub(pattern, '', translated_text, flags=re.IGNORECASE)
        
        # 应用邮箱过滤
        translated_text = re.sub(email_pattern, '', translated_text)
        
        # 应用社交媒体过滤
        for pattern in social_patterns:
            translated_text = re.sub(pattern, '', translated_text)
        
        # 应用互动提示过滤
        for pattern in interaction_patterns:
            translated_text = re.sub(pattern, '', translated_text, flags=re.IGNORECASE)
        
        # 最终清理：再次确保移除任何残留的说明性文字
        final_cleanup_patterns = [
            r'（.*?已.*?除.*?）',               # 匹配"已...除"模式
            r'\(.*?已.*?除.*?\)',               # 英文括号版本
            r'（.*?contact.*?）',               # 联系相关
            r'\(.*?contact.*?\)',               # 英文括号联系相关
        ]
        
        for pattern in final_cleanup_patterns:
            translated_text = re.sub(pattern, '', translated_text, flags=re.IGNORECASE)
        
        # 清理多余的空白字符
        translated_text = re.sub(r'\s+', ' ', translated_text)  # 多个空格合并为一个
        translated_text = re.sub(r'\n{3,}', '\n\n', translated_text)  # 多个换行合并
        translated_text = translated_text.strip()  # 去除首尾空白
        
        logger.info("已过滤URL、域名、邮箱地址和社交媒体引用")
        
        # 处理字符限制
        if task_id and "title" in task_id.lower():
            # 如果是标题，限制为50个字符
            if len(translated_text) > 50:
                logger.info(f"标题超过AcFun限制(50字符)，将被截断: {len(translated_text)} -> 50")
                translated_text = translated_text[:50]
        else:
            # 如果是描述，限制为1000个字符
            if len(translated_text) > 1000:
                logger.info(f"描述超过AcFun限制(1000字符)，将被截断: {len(translated_text)} -> 1000")
                translated_text = translated_text[:997] + "..."
        
        return translated_text
    
    except Exception as e:
        logger.error(f"翻译过程中发生错误: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def generate_acfun_tags(title, description, openai_config=None, task_id=None):
    """
    使用OpenAI生成AcFun风格的标签
    
    Args:
        title (str): 视频标题
        description (str): 视频描述
        openai_config (dict): OpenAI配置信息，包含api_key, base_url, model_name等
        task_id (str, optional): 任务ID，用于日志记录
        
    Returns:
        list: 标签列表，出错时返回空列表
    """
    logger = setup_task_logger(task_id or "unknown")
    logger.info(f"开始生成AcFun标签")
    logger.info(f"视频标题: {title}")
    logger.info(f"视频描述 (截取前100字符用于显示): {description[:100]}...")
    logger.info(f"视频描述总长度: {len(description)} 字符")
    
    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.error("缺少OpenAI配置或API密钥")
        return []
    
    try:
        # 获取OpenAI客户端 (1.x版本)
        client = get_openai_client(openai_config)
        model_name = openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
        
        # 构建标签生成提示
        prompt = f"""根据以下视频的标题和描述，生成恰好6个适合AcFun平台的标签。
        要求:
        - 必须生成6个标签，不多不少
        - 每个标签长度不超过10个汉字或20个字符
        - 标签应反映视频的核心内容、类型或情感
        - 避免过于宽泛的标签如"搞笑"、"有趣"等
        - 包含1-2个与视频主题相关的基础关键词
        
        视频标题:
        {title}
        
        视频描述:
        {description}
        
        JSON格式返回6个标签，例如:
        ["标签1", "标签2", "标签3", "标签4", "标签5", "标签6"]
        
        只返回JSON数组，不要有其他内容:
        """
        
        start_time = time.time()
        
        # 使用新版API调用格式
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "你是一个内容标签生成工具。你的任务是为视频内容生成恰当的标签，以帮助用户更好地发现和分类内容。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=800
        )
        
        response_time = time.time() - start_time
        logger.info(f"标签生成完成，耗时: {response_time:.2f}秒")
        
        # 提取响应内容
        tags_response = response.choices[0].message.content.strip()
        
        # 尝试解析JSON
        import json
        import re
        
        # 清理响应文本，确保它是有效的JSON
        # 有时API可能返回带有额外文本的JSON，尝试提取JSON部分
        json_pattern = r'\[.*?\]'
        json_match = re.search(json_pattern, tags_response, re.DOTALL)
        
        if json_match:
            try:
                tags = json.loads(json_match.group())
                # 确保我们有6个标签
                if len(tags) > 6:
                    tags = tags[:6]
                elif len(tags) < 6:
                    # 如果少于6个，用空字符串填充
                    tags.extend([''] * (6 - len(tags)))
                
                # 确保每个标签不超过长度限制
                tags = [tag[:20] for tag in tags]
                
                logger.info(f"生成标签: {tags}")
                return tags
            except json.JSONDecodeError as e:
                logger.error(f"解析标签JSON时出错: {str(e)}")
                logger.error(f"原始响应: {tags_response}")
        else:
            logger.error(f"无法从响应中提取JSON数组: {tags_response}")
        
        return []
    
    except Exception as e:
        logger.error(f"生成标签过程中发生错误: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return []

def flatten_partitions(id_mapping_data):
    """
    将id_mapping_data扁平化为分区列表
    
    Args:
        id_mapping_data (list): id_mapping.json解析后的数据
        
    Returns:
        list: 分区列表，每个元素包含id, name等信息
    """
    if not id_mapping_data:
        return []
        
    partitions = []
    
    for category_item in id_mapping_data:
        # 兼容两种格式："name"或"category"作为分类名称
        category_name = category_item.get('name', '') or category_item.get('category', '')
        for partition in category_item.get('partitions', []):
            # 记录一级分区信息
            partition_id = partition.get('id')
            partition_name = partition.get('name', '')
            partition_desc = partition.get('description', '')
            
            if partition_id:
                partitions.append({
                    'id': partition_id,
                    'name': partition_name,
                    'description': partition_desc,
                    'parent_name': category_name
                })
            
            # 处理二级分区
            for sub_partition in partition.get('sub_partitions', []):
                sub_id = sub_partition.get('id')
                sub_name = sub_partition.get('name', '')
                sub_desc = sub_partition.get('description', '')
                
                if sub_id:
                    partitions.append({
                        'id': sub_id,
                        'name': sub_name,
                        'description': sub_desc,
                        'parent_name': partition_name
                    })
    
    return partitions

def recommend_acfun_partition(title, description, id_mapping_data, openai_config=None, task_id=None):
    """
    使用OpenAI推荐AcFun视频分区
    
    Args:
        title (str): 视频标题
        description (str): 视频描述
        id_mapping_data (list): 分区ID映射数据
        openai_config (dict): OpenAI配置信息
        task_id (str, optional): 任务ID，用于日志记录
        
    Returns:
        str or None: 推荐分区ID，出错时返回None
    """
    logger = setup_task_logger(task_id or "unknown")
    logger.info(f"开始推荐AcFun视频分区")
    
    # 检查必要信息
    if not title and not description:
        logger.warning("缺少标题和描述，无法推荐分区")
        return None
    
    if not id_mapping_data:
        logger.warning("缺少分区映射数据 (id_mapping_data is empty or None)，无法推荐分区")
        return None
    
    if not openai_config or not openai_config.get('OPENAI_API_KEY'):
        logger.warning("缺少OpenAI配置或API密钥，无法推荐分区")
        return None
    
    # 将分区数据扁平化为易于处理的列表
    partitions = flatten_partitions(id_mapping_data)
    if not partitions:
        logger.warning("分区映射数据格式错误或为空 (flatten_partitions returned empty list)，无法推荐分区")
        return None
    
    try:
        # 获取OpenAI客户端 (1.x版本)
        client = get_openai_client(openai_config)
        model_name = openai_config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
        
        # 准备分区描述信息
        partitions_info = []
        for p in partitions:
            parent_name = p.get('parent_name', '') 
            prefix = f"{parent_name} - " if parent_name else ""
            partitions_info.append(f"{prefix}{p['name']} (ID: {p['id']}): {p.get('description', '无描述')}")
        
        partitions_text = "\n".join(partitions_info)
        
        # 构建提示内容
        prompt = f"""请根据以下视频的标题和描述，从给定的AcFun分区列表中，选择最合适的一个分区。

视频标题: {title}

视频描述: {description[:500] + '...' if len(description) > 500 else description}

AcFun分区列表:
{partitions_text}

要求:
1. 只能选择上述列表中的一个分区
2. 分析视频内容与分区的匹配度
3. 只返回一个分区ID，格式为:
{{"id": "分区ID", "reason": "简要推荐理由"}}

不要返回任何其他格式或额外内容。
"""
        
        # 使用新版API调用格式
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "你是一个专业视频分类助手，擅长将视频内容匹配到最合适的分区。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=800
        )
        
        result = response.choices[0].message.content.strip()
        logger.info(f"分区推荐原始响应: {result}")
        
        # 解析结果
        import json
        import re
        
        available_partition_ids = [p['id'] for p in partitions]

        # 尝试直接解析JSON
        try:
            data = json.loads(result)
            if 'id' in data:
                # 验证ID是否存在于分区列表中
                partition_id = str(data['id'])
                if partition_id in available_partition_ids:
                    logger.info(f"推荐分区: ID {partition_id}, 理由: {data.get('reason', '无')}")
                    # 直接返回分区ID字符串，而不是整个字典
                    return partition_id
                else:
                    logger.warning(f"推荐的分区ID '{partition_id}' 不在有效分区列表中。可用ID: {available_partition_ids}。原始响应: {result}")
        except json.JSONDecodeError as e_direct:
            logger.warning(f"直接解析JSON响应失败: {e_direct}. 原始响应: {result}")
            # 如果直接解析失败，尝试从文本中提取JSON
            match = re.search(r'\\{.*\\}', result, re.DOTALL)
            if match:
                extracted_json_text = match.group(0)
                try:
                    data = json.loads(extracted_json_text)
                    if 'id' in data:
                        # 验证ID是否存在于分区列表中
                        partition_id = str(data['id'])
                        if partition_id in available_partition_ids:
                            logger.info(f"从提取内容中推荐分区: ID {partition_id}, 理由: {data.get('reason', '无')}")
                            # 直接返回分区ID字符串，而不是整个字典
                            return partition_id
                        else:
                            logger.warning(f"提取内容中推荐的分区ID '{partition_id}' 不在有效分区列表中。可用ID: {available_partition_ids}。提取的文本: {extracted_json_text}")
                except json.JSONDecodeError as e_extract:
                    logger.warning(f"无法从提取的文本中解析JSON: {e_extract}. 提取的文本: {extracted_json_text}")
        
        # 如果上述方法都失败，尝试提取ID
        id_match = re.search(r'"id"\\s*:\\s*"?(\\d+)"?', result)
        if id_match:
            partition_id = id_match.group(1)
            if partition_id in available_partition_ids:
                reason_match = re.search(r'"reason"\\s*:\\s*"([^"]+)"', result)
                reason = reason_match.group(1) if reason_match else "未提供理由 (正则提取)"
                logger.info(f"正则提取的推荐分区: ID {partition_id}, 理由: {reason}")
                return partition_id
            else:
                logger.warning(f"正则提取的分区ID '{partition_id}' 不在有效分区列表中。可用ID: {available_partition_ids}。原始响应: {result}")
        
        logger.warning(f"无法从OpenAI响应中解析或验证有效的分区ID。最终原始响应: {result}")
        return None
        
    except Exception as e:
        logger.error(f"推荐分区过程中发生严重错误: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return None 