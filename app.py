#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging
import uuid
import shutil
import time
import datetime
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file
from flask_cors import CORS, cross_origin
from modules.youtube_handler import download_video_data
from modules.utils import parse_id_md_to_json, process_cover
from modules.config_manager import load_config, update_config, DEFAULT_CONFIG
from modules.task_manager import add_task, start_task, get_task, get_all_tasks, get_tasks_by_status, update_task, delete_task, force_upload_task, TASK_STATES, clear_all_tasks
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = os.urandom(24)  # 用于flash消息
app.jinja_env.globals.update(now=datetime.now())  # 添加当前时间到模板全局变量

# 配置CORS，允许来自YouTube的跨域请求
CORS(app, resources={
    r"/tasks/add_via_extension": {
        "origins": ["*://www.youtube.com", "*://youtube.com", "https://www.youtube.com", "https://youtube.com"],
        "methods": ["POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

# 确保日志目录存在
log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(log_dir, exist_ok=True)

# 配置日志
log_file = os.path.join(log_dir, 'app.log')
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# 文件处理器
file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=10, encoding='utf-8')
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.WARNING)

# 控制台处理器
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.WARNING)
# 确保Windows控制台编码正确
if os.name == 'nt':
    import sys
    import codecs
    # 强制设置stdout和stderr为UTF-8编码
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    # 设置环境变量
    os.environ["PYTHONIOENCODING"] = "utf-8"
    # 为控制台处理器设置编码
    console_handler.setStream(codecs.getwriter('utf-8')(sys.stdout.buffer))

# 配置根日志记录器
root_logger = logging.getLogger()
root_logger.setLevel(logging.WARNING)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# 强制设置所有日志记录器的默认编码为UTF-8
logging.getLogger().handlers[0].encoding = 'utf-8'
if len(logging.getLogger().handlers) > 1:
    logging.getLogger().handlers[1].encoding = 'utf-8'

# 配置应用日志记录器
logger = logging.getLogger('Y2A-Auto')
logger.setLevel(logging.WARNING)

# 静态目录变量（保留用于兼容性）
covers_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'covers')

def init_id_mapping():
    """
    初始化AcFun分区ID映射.
    id_mapping.json 文件现在应该由 acfunid/ 目录直接提供，并包含在Docker镜像中。
    此函数仅记录一条信息，不再执行文件生成或检查逻辑。
    """
    logger.info("AcFun分区ID映射 (id_mapping.json) 应由 'acfunid/' 目录提供。")
    # 旧的检查和生成逻辑已被移除

# 模板辅助函数
def task_status_display(status):
    """将任务状态代码转换为显示文本"""
    status_map = {
        TASK_STATES['PENDING']: '等待处理',
        TASK_STATES['DOWNLOADING']: '下载中',
        TASK_STATES['DOWNLOADED']: '下载完成',
        TASK_STATES['TRANSLATING_SUBTITLE']: '翻译字幕中',
        TASK_STATES['TRANSLATING']: '翻译中',
        TASK_STATES['TAGGING']: '生成标签中',
        TASK_STATES['PARTITIONING']: '推荐分区中',
        TASK_STATES['MODERATING']: '内容审核中',
        TASK_STATES['AWAITING_REVIEW']: '等待人工审核',
        TASK_STATES['READY_FOR_UPLOAD']: '准备上传',
        TASK_STATES['UPLOADING']: '上传中',
        TASK_STATES['COMPLETED']: '已完成',
        TASK_STATES['FAILED']: '失败',
        'fetching_info': '采集信息中',
        'info_fetched': '信息已采集',
    }
    return status_map.get(status, status)

def task_status_color(status):
    """将任务状态代码转换为显示颜色"""
    color_map = {
        TASK_STATES['PENDING']: 'secondary',
        TASK_STATES['DOWNLOADING']: 'info',
        TASK_STATES['DOWNLOADED']: 'info',
        TASK_STATES['TRANSLATING_SUBTITLE']: 'info',
        TASK_STATES['TRANSLATING']: 'info',
        TASK_STATES['TAGGING']: 'info',
        TASK_STATES['PARTITIONING']: 'info',
        TASK_STATES['MODERATING']: 'info',
        TASK_STATES['AWAITING_REVIEW']: 'warning',
        TASK_STATES['READY_FOR_UPLOAD']: 'primary',
        TASK_STATES['UPLOADING']: 'primary',
        TASK_STATES['COMPLETED']: 'success',
        TASK_STATES['FAILED']: 'danger'
    }
    return color_map.get(status, 'secondary')

def get_partition_name(partition_id):
    """根据分区ID获取分区名称"""
    if not partition_id:
        return None
    
    # 读取分区映射数据 - 修改路径为 acfunid
    id_mapping_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'acfunid', 'id_mapping.json')
    try:
        with open(id_mapping_path, 'r', encoding='utf-8') as f:
            id_mapping = json.load(f)
            
        # 遍历查找匹配的分区ID
        for category in id_mapping:
            for partition in category.get('partitions', []):
                if partition.get('id') == partition_id:
                    return partition.get('name')
                
                # 检查子分区
                for sub_partition in partition.get('sub_partitions', []):
                    if sub_partition.get('id') == partition_id:
                        return sub_partition.get('name')
    except Exception as e:
        logger.error(f"获取分区名称时出错: {str(e)}")
    
    return None

def parse_json(json_str):
    """将JSON字符串解析为Python对象"""
    if not json_str:
        return {}  # 返回空字典
    
    try:
        return json.loads(json_str)
    except Exception as e:
        logger.error(f"解析JSON时出错: {str(e)}")
        return {} # 返回空字典

# 注册模板辅助函数
app.jinja_env.globals.update(
    task_status_display=task_status_display,
    task_status_color=task_status_color,
    get_partition_name=get_partition_name,
    parse_json=parse_json
)

ALIYUN_LABEL_MAP = {
    "pornographic_adult": "疑似色情内容",
    "sexual_terms": "疑似性健康内容",
    "sexual_suggestive": "疑似低俗内容",
    "political_figure": "疑似政治人物",
    "political_entity": "疑似政治实体",
    "political_n": "疑似敏感政治内容",
    "political_p": "疑似涉政禁宣人物",
    "political_a": "涉政专项升级保障",
    "violent_extremist": "疑似极端组织",
    "violent_incidents": "疑似极端主义内容",
    "violent_weapons": "疑似武器弹药",
    "contraband_drug": "疑似毒品相关",
    "contraband_gambling": "疑似赌博相关",
    "contraband_act": "疑似违禁行为",
    "contraband_entity": "疑似违禁工具",
    "inappropriate_discrimination": "疑似偏见歧视内容",
    "inappropriate_ethics": "疑似不良价值观内容",
    "inappropriate_profanity": "疑似攻击辱骂内容",
    "inappropriate_oral": "疑似低俗口头语内容",
    "inappropriate_superstition": "疑似封建迷信内容",
    "inappropriate_nonsense": "疑似无意义灌水内容",
    "pt_to_sites": "疑似站外引流",
    "pt_by_recruitment": "疑似网赚兼职广告",
    "pt_to_contact": "疑似引流广告号",
    "religion_b": "疑似涉及佛教",
    "religion_t": "疑似涉及道教",
    "religion_c": "疑似涉及基督教",
    "religion_i": "疑似涉及伊斯兰教",
    "religion_h": "疑似涉及印度教",
    "customized": "命中自定义词库",
    "nonLabel": "内容正常", # 通常表示无风险
    "normal": "内容正常" # 另一种表示无风险的标签
    # 可以根据需要添加更多映射
}

def get_aliyun_label_chinese(label_value):
    """获取阿里云审核标签的中文含义"""
    return ALIYUN_LABEL_MAP.get(label_value, label_value) # 如果找不到映射，返回原始标签

# 注册模板辅助函数
app.jinja_env.globals.update(
    task_status_display=task_status_display,
    task_status_color=task_status_color,
    get_partition_name=get_partition_name,
    parse_json=parse_json,
    get_aliyun_label_chinese=get_aliyun_label_chinese # 添加新的辅助函数
)

@app.route('/')
def index():
    """首页"""
    logger.info("访问首页")
    return render_template('index.html')

@app.route('/tasks')
def tasks():
    """任务列表页面"""
    logger.info("访问任务列表页面")
    tasks_list = get_all_tasks()
    return render_template('tasks.html', tasks=tasks_list)

@app.route('/manual_review')
def manual_review():
    """人工审核列表页面"""
    logger.info("访问人工审核列表页面")
    review_tasks = get_tasks_by_status(TASK_STATES['AWAITING_REVIEW'])
    
    # 封面图片现在直接从downloads目录提供
    
    return render_template('manual_review.html', tasks=review_tasks)

@app.route('/tasks/<task_id>/edit', methods=['GET', 'POST'])
def edit_task(task_id):
    """任务编辑页面"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('tasks'))
    
    if request.method == 'POST':
        # 处理表单提交
        video_title = request.form.get('video_title_translated', '')
        description = request.form.get('description_translated', '')
        partition_id = request.form.get('selected_partition_id', '')
        tags_json = request.form.get('tags_json', '[]')
        
        # 更新任务信息
        update_data = {
            'video_title_translated': video_title,
            'description_translated': description,
            'selected_partition_id': partition_id,
            'tags_generated': tags_json
        }
        
        if task['status'] == TASK_STATES['AWAITING_REVIEW']:
            # 如果是从等待审核状态修改，则设置为等待上传
            update_data['status'] = TASK_STATES['PENDING']
        
        update_task(task_id, **update_data)
        logger.info(f"任务 {task_id} 信息已更新")
        
        # 执行上传操作（当任务处于"已完成"、"等待处理"或"准备上传"状态时）
        task = get_task(task_id)  # 重新获取更新后的任务信息
        if task['status'] in [TASK_STATES['COMPLETED'], TASK_STATES['PENDING'], TASK_STATES['READY_FOR_UPLOAD']]:
            # 获取当前配置
            config = load_config()
            
            # 启动后台上传
            logger.info(f"开始后台上传任务 {task_id} 到AcFun")
            flash('任务已保存，正在后台上传到AcFun...', 'info')
            
            import threading
            
            def background_upload():
                """后台上传函数"""
                try:
                    # 调用上传函数
                    success = force_upload_task(task_id, config)
                    
                    if success:
                        logger.info(f"任务 {task_id} 后台上传成功")
                    else:
                        logger.error(f"任务 {task_id} 后台上传失败")
                except Exception as e:
                    logger.error(f"任务 {task_id} 后台上传出错: {str(e)}")
                    import traceback
                    logger.error(traceback.format_exc())
            
            # 启动后台线程
            upload_thread = threading.Thread(target=background_upload, daemon=True)
            upload_thread.start()
        else:
            flash('任务已保存，但尚未完成处理，无法上传', 'warning')
        
        return redirect(url_for('tasks'))
    
    # GET请求，显示编辑页面
    # 封面图片现在直接从downloads目录提供
    
    # 读取分区映射数据
    id_mapping_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'acfunid', 'id_mapping.json')
    id_mapping = []
    try:
        with open(id_mapping_path, 'r', encoding='utf-8') as f:
            id_mapping = json.load(f)
    except Exception as e:
        logger.error(f"读取分区映射数据失败: {str(e)}")
    
    # 准备标签字符串
    tags_string = ""
    if task.get('tags_generated'):
        try:
            tags = json.loads(task['tags_generated'])
            tags_string = ", ".join(tags)
        except Exception as e:
            logger.error(f"解析标签JSON失败: {str(e)}")
    
    # 获取当前配置
    config = load_config()
    
    return render_template(
        'edit_task.html', 
        task=task, 
        id_mapping=id_mapping, 
        tags_string=tags_string,
        config=config
    )

@app.route('/tasks/<task_id>/review')
def review_task(task_id):
    """重定向到任务编辑页面"""
    return redirect(url_for('edit_task', task_id=task_id))

@app.route('/tasks/add', methods=['POST'])
def add_task_route():
    """添加新任务"""
    youtube_url = request.form.get('youtube_url')
    
    if not youtube_url:
        flash('YouTube URL不能为空', 'danger')
        return redirect(url_for('tasks'))
    
    task_id = add_task(youtube_url)
    
    if task_id:
        # 获取当前配置
        config = load_config()
        
        # 如果启用了自动模式，立即开始处理任务
        if config.get('AUTO_MODE_ENABLED', False):
            logger.info(f"自动模式已启用，立即开始处理任务 {task_id}")
            start_task(task_id, config)
            flash(f'任务已添加并开始处理: {youtube_url}', 'success')
        else:
            flash(f'任务已添加: {youtube_url}', 'success')
    else:
        flash(f'添加任务失败: {youtube_url}', 'danger')
    
    return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/start', methods=['POST'])
def start_task_route(task_id):
    """开始处理任务"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('tasks'))
    
    if task['status'] not in [TASK_STATES['PENDING'], TASK_STATES['FAILED']]:
        flash(f'当前任务状态为 {task_status_display(task["status"])}，不能启动', 'warning')
        return redirect(url_for('tasks'))
    
    # 获取当前配置
    config = load_config()
    
    # 启动任务处理
    success = start_task(task_id, config)
    
    if success:
        # 检查是否是自动模式
        if config.get('AUTO_MODE_ENABLED', False):
            flash('任务已启动，自动模式将会自动完成下载、处理和上传', 'info')
            
            # 等待一段时间后检查任务状态
            import threading
            
            # 使用传统页面刷新方式
        else:
            flash('任务处理已启动', 'success')
    else:
        flash('启动任务处理失败', 'danger')
    
    return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/delete', methods=['POST'])
def delete_task_route(task_id):
    """删除任务"""
    delete_files = request.form.get('delete_files', 'true').lower() in ('true', 'yes', '1', 'on')
    
    success = delete_task(task_id, delete_files)
    
    if success:
        flash('任务已删除', 'success')
    else:
        flash('删除任务失败', 'danger')
    
    return redirect(url_for('tasks'))

@app.route('/tasks/<task_id>/force_upload', methods=['POST'])
def force_upload_task_route(task_id):
    """强制上传任务"""
    task = get_task(task_id)
    
    if not task:
        flash('任务不存在', 'danger')
        return redirect(url_for('manual_review'))
    
    # 获取当前配置
    config = load_config()
    
    # 启动后台强制上传
    logger.info(f"开始后台强制上传任务 {task_id} 到AcFun")
    flash('已启动强制上传，正在后台处理...', 'info')
    
    import threading
    
    def background_force_upload():
        """后台强制上传函数"""
        try:
            # 调用上传函数
            success = force_upload_task(task_id, config)
                
            if success:
                logger.info(f"任务 {task_id} 后台强制上传成功")
            else:
                logger.error(f"任务 {task_id} 后台强制上传失败")
        except Exception as e:
            logger.error(f"任务 {task_id} 后台强制上传出错: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
    
    # 启动后台线程
    upload_thread = threading.Thread(target=background_force_upload, daemon=True)
    upload_thread.start()
    
    return redirect(url_for('manual_review'))

@app.route('/tasks/<task_id>/abandon', methods=['POST'])
def abandon_task_route(task_id):
    """放弃任务"""
    delete_files = request.form.get('delete_files', 'true').lower() in ('true', 'yes', '1', 'on')
    
    # 更新任务状态为失败
    update_task(task_id, status=TASK_STATES['FAILED'], error_message="用户主动放弃任务")
    
    if delete_files:
        # 删除任务文件
        from modules.task_manager import delete_task_files
        delete_task_files(task_id)
    
    flash('任务已放弃', 'warning')
    return redirect(url_for('manual_review'))

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    """设置页面，用于管理配置"""
    if request.method == 'POST':
        # 处理表单提交
        form_data = request.form.to_dict()
        
        # 处理复选框（未选中时不会提交）
        checkboxes = [
            'AUTO_MODE_ENABLED', 'TRANSLATE_TITLE', 'TRANSLATE_DESCRIPTION',
            'GENERATE_TAGS', 'RECOMMEND_PARTITION', 'CONTENT_MODERATION_ENABLED',
            'LOG_CLEANUP_ENABLED'
        ]
        for checkbox in checkboxes:
            if checkbox not in form_data:
                form_data[checkbox] = 'off'  # 未选中的复选框
        
        # 处理文件上传
        cookies_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies')
        os.makedirs(cookies_dir, exist_ok=True)
        
        # 处理YouTube Cookies文件上传
        if 'youtube_cookies_file' in request.files:
            cookies_file = request.files['youtube_cookies_file']
            if cookies_file.filename:
                # 保存到cookies目录
                yt_cookies_path = os.path.join(cookies_dir, 'yt_cookies.txt')
                cookies_file.save(yt_cookies_path)
                
                # 更新配置中的路径
                form_data['YOUTUBE_COOKIES_PATH'] = 'cookies/yt_cookies.txt'
                
                logger.info(f"YouTube cookies文件已上传并保存到: {yt_cookies_path}")
        
        # 处理AcFun Cookies文件上传
        if 'acfun_cookies_file' in request.files:
            cookies_file = request.files['acfun_cookies_file']
            if cookies_file.filename:
                # 保存到cookies目录
                ac_cookies_path = os.path.join(cookies_dir, 'ac_cookies.txt')
                cookies_file.save(ac_cookies_path)
                
                # 更新配置中的路径
                form_data['ACFUN_COOKIES_PATH'] = 'cookies/ac_cookies.txt'
                
                logger.info(f"AcFun cookies文件已上传并保存到: {ac_cookies_path}")
        
        # 更新配置
        update_config(form_data)
        flash('配置已成功保存', 'success')
        return redirect(url_for('settings'))
    
    # GET请求，显示设置页面
    config = load_config()
    return render_template('settings.html', config=config)

@app.route('/settings/update_cover_mode', methods=['POST'])
def update_cover_mode():
    """更新封面处理模式"""
    try:
        data = request.get_json()
        mode = data.get('mode')
        task_id = data.get('task_id')
        
        if mode not in ('crop', 'pad'):
            return jsonify({"success": False, "message": "无效的处理模式"}), 400
        
        # 更新全局配置
        config = load_config()
        config['COVER_PROCESSING_MODE'] = mode
        update_config(config)
        
        # 如果提供了任务ID，则处理该任务的封面
        if task_id:
            task = get_task(task_id)
            if task and task.get('cover_path_local') and os.path.exists(task['cover_path_local']):
                # 直接在downloads目录中处理封面
                process_cover(task['cover_path_local'], mode=mode)
        
        return jsonify({"success": True, "message": "封面处理模式已更新"})
    except Exception as e:
        logger.error(f"更新封面处理模式失败: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/test_download')
def test_download():
    """测试YouTube视频下载功能"""
    url = request.args.get('url')
    config = load_config()
    cookies_path = config.get('YOUTUBE_COOKIES_PATH')
    
    if not url:
        return jsonify({"success": False, "error": "缺少url参数"}), 400
    
    # 创建唯一任务ID
    task_id = str(uuid.uuid4())
    logger.info(f"开始测试下载，URL: {url}, 任务ID: {task_id}")
    
    # 调用下载函数
    success, result = download_video_data(url, task_id, cookies_path)
    
    if success:
        return jsonify({
            "success": True,
            "task_id": task_id,
            "result": {
                "video_path": os.path.basename(result["video_path"]),
                "metadata_path": os.path.basename(result["metadata_path"]) if result["metadata_path"] else None,
                "cover_path": os.path.basename(result["cover_path"]) if result["cover_path"] else None,
                "subtitles_paths": [os.path.basename(p) for p in result["subtitles_paths"]],
                "task_dir": result["task_dir"]
            }
        })
    else:
        return jsonify({"success": False, "error": result}), 500

@app.route('/tasks/add_via_extension', methods=['POST', 'OPTIONS'])
@cross_origin(origins=["*://www.youtube.com", "*://youtube.com", "https://www.youtube.com", "https://youtube.com"])
def add_task_via_extension():
    """
    接收来自浏览器扩展的任务添加请求
    """
    if request.method == 'OPTIONS':
        # 预检请求处理
        return '', 204
    
    try:
        data = request.get_json()
        if not data or 'youtube_url' not in data:
            logger.error("请求数据无效或缺少youtube_url")
            return jsonify({
                "success": False,
                "message": "请求格式错误，缺少youtube_url字段"
            }), 400
        
        youtube_url = data['youtube_url']
        logger.info(f"从浏览器扩展接收到添加任务请求: {youtube_url}")
        
        # 调用任务管理器添加任务
        task_id = add_task(youtube_url)
        
        if task_id:
            # 获取当前配置
            config = load_config()
            
            # 如果启用了自动模式，立即开始处理任务
            if config.get('AUTO_MODE_ENABLED', False):
                logger.info(f"自动模式已启用，立即开始处理任务 {task_id}")
                start_task(task_id, config)
                status_message = "任务已添加并开始处理"
            else:
                status_message = "任务已添加到队列"
            
            return jsonify({
                "success": True,
                "message": status_message,
                "task_id": task_id
            })
        else:
            return jsonify({
                "success": False,
                "message": "添加任务失败，请检查服务器日志"
            }), 500
    
    except Exception as e:
        logger.error(f"处理扩展请求时发生错误: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"处理请求时发生错误: {str(e)}"
        }), 500

@app.route('/tasks/clear_all', methods=['POST'])
def clear_all_tasks_route():
    """清除所有任务"""
    delete_files = request.form.get('delete_files', 'true').lower() in ('true', 'yes', '1', 'on')
    
    success = clear_all_tasks(delete_files)
    
    if success:
        flash('所有任务已清除', 'success')
    else:
        flash('清除所有任务失败', 'danger')
    
    return redirect(url_for('tasks'))

@app.route('/covers/<task_id>')
def get_task_cover(task_id):
    """获取任务封面图片"""
    try:
        # 获取任务信息
        task = get_task(task_id)
        if not task:
            logger.warning(f"任务 {task_id} 不存在")
            return '', 404
        
        # 检查封面文件路径
        cover_path = task.get('cover_path_local')
        if not cover_path or not os.path.exists(cover_path):
            logger.warning(f"任务 {task_id} 的封面文件不存在: {cover_path}")
            return '', 404
        
        # 直接从downloads目录提供文件
        return send_file(cover_path, mimetype='image/jpeg')
        
    except Exception as e:
        logger.error(f"获取任务 {task_id} 封面时出错: {str(e)}")
        return '', 500

# 配置app
def configure_app(app, config_data):
    """
    配置Flask应用
    
    Args:
        app: Flask应用实例
        config_data: 配置数据
    """
    app.config['OPENAI_API_KEY'] = config_data.get('OPENAI_API_KEY', '')
    app.config['OPENAI_BASE_URL'] = config_data.get('OPENAI_BASE_URL', '')
    app.config['OPENAI_MODEL_NAME'] = config_data.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo')
    
    # 不再使用proxies参数，新版OpenAI客户端不支持
    # 如果需要代理，通过环境变量设置: HTTP_PROXY, HTTPS_PROXY

    app.config['ALIYUN_ACCESS_KEY_ID'] = config_data.get('ALIYUN_ACCESS_KEY_ID', '')
    app.config['ALIYUN_ACCESS_KEY_SECRET'] = config_data.get('ALIYUN_ACCESS_KEY_SECRET', '')
    app.config['ALIYUN_CONTENT_MODERATION_REGION'] = config_data.get('ALIYUN_CONTENT_MODERATION_REGION', 'cn-shanghai')
    
    app.config['ACFUN_USERNAME'] = config_data.get('ACFUN_USERNAME', '')
    app.config['ACFUN_PASSWORD'] = config_data.get('ACFUN_PASSWORD', '')

# 使用传统页面刷新方式

# 日志清理功能
def cleanup_logs(hours=168):
    """
    清理指定小时数以前的日志文件
    
    Args:
        hours: 保留最近多少小时的日志
    
    Returns:
        cleanup_stats: 清理统计信息
    """
    try:
        logger.info(f"开始清理{hours}小时前的日志文件")
        cutoff_date = datetime.now() - timedelta(hours=hours)
        cutoff_timestamp = cutoff_date.timestamp()
        
        files_removed = 0
        bytes_freed = 0
        
        # 遍历日志目录
        for filename in os.listdir(log_dir):
            file_path = os.path.join(log_dir, filename)
            
            # 只处理文件，不处理目录
            if os.path.isfile(file_path):
                # 获取文件修改时间
                file_mtime = os.path.getmtime(file_path)
                
                # 如果文件修改时间早于截止日期，则删除
                if file_mtime < cutoff_timestamp:
                    # 获取文件大小
                    file_size = os.path.getsize(file_path)
                    
                    # 删除文件
                    os.remove(file_path)
                    
                    # 更新统计信息
                    files_removed += 1
                    bytes_freed += file_size
                    
                    logger.info(f"已删除日志文件: {filename}")
        
        # 转换字节为可读大小
        if bytes_freed < 1024:
            bytes_freed_str = f"{bytes_freed} 字节"
        elif bytes_freed < 1024 * 1024:
            bytes_freed_str = f"{bytes_freed / 1024:.2f} KB"
        elif bytes_freed < 1024 * 1024 * 1024:
            bytes_freed_str = f"{bytes_freed / (1024 * 1024):.2f} MB"
        else:
            bytes_freed_str = f"{bytes_freed / (1024 * 1024 * 1024):.2f} GB"
            
        logger.info(f"日志清理完成，已删除{files_removed}个文件，释放{bytes_freed_str}")
        
        return {
            "success": True,
            "files_removed": files_removed,
            "bytes_freed": bytes_freed,
            "bytes_freed_readable": bytes_freed_str,
            "cutoff_date": cutoff_date.strftime("%Y-%m-%d %H:%M:%S")
        }
    except Exception as e:
        logger.error(f"清理日志文件时出错: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }

def clear_specific_logs():
    """
    立即清空特定的日志文件（清空主要日志，删除任务日志）
    
    Returns:
        clear_stats: 清理统计信息
    """
    try:
        logger.info("开始清空特定日志文件")
        
        files_processed = 0
        bytes_freed = 0
        processed_files = []
        
        # 定义需要清空内容的日志文件（保留文件，只清空内容）
        clear_files = ['task_manager.log', 'app.log']
        
        # 先处理固定名称的日志文件 - 清空内容
        for filename in clear_files:
            file_path = os.path.join(log_dir, filename)
            if os.path.exists(file_path):
                try:
                    # 获取文件大小
                    file_size = os.path.getsize(file_path)
                    
                    # 清空文件内容
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write('')
                    
                    files_processed += 1
                    bytes_freed += file_size
                    processed_files.append(f"{filename} (已清空)")
                    logger.info(f"已清空日志文件: {filename}")
                except Exception as e:
                    logger.error(f"清空文件 {filename} 失败: {str(e)}")
        
        # 处理任务日志文件 (格式如: task_xxx.log) - 删除文件
        task_files_to_delete = []
        for filename in os.listdir(log_dir):
            file_path = os.path.join(log_dir, filename)
            
            # 检查是否是任务日志文件
            if (os.path.isfile(file_path) and 
                filename.startswith('task_') and 
                filename.endswith('.log') and
                filename not in clear_files):
                task_files_to_delete.append((filename, file_path))
        
        # 删除任务日志文件
        for filename, file_path in task_files_to_delete:
            try:
                # 获取文件大小
                file_size = os.path.getsize(file_path)
                
                # 删除文件
                os.remove(file_path)
                
                files_processed += 1
                bytes_freed += file_size
                processed_files.append(f"{filename} (已删除)")
                logger.info(f"已删除任务日志文件: {filename}")
            except Exception as e:
                logger.error(f"删除文件 {filename} 失败: {str(e)}")
        
        # 转换字节为可读大小
        if bytes_freed < 1024:
            bytes_freed_str = f"{bytes_freed} 字节"
        elif bytes_freed < 1024 * 1024:
            bytes_freed_str = f"{bytes_freed / 1024:.2f} KB"
        elif bytes_freed < 1024 * 1024 * 1024:
            bytes_freed_str = f"{bytes_freed / (1024 * 1024):.2f} MB"
        else:
            bytes_freed_str = f"{bytes_freed / (1024 * 1024 * 1024):.2f} GB"
            
        logger.info(f"日志清理完成，已处理{files_processed}个文件，释放{bytes_freed_str}")
        
        return {
            "success": True,
            "files_processed": files_processed,
            "bytes_freed": bytes_freed,
            "bytes_freed_readable": bytes_freed_str,
            "processed_files": processed_files
        }
    except Exception as e:
        logger.error(f"清理日志文件时出错: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }

def schedule_log_cleanup():
    """根据配置设置日志清理定时任务"""
    config = load_config()
    
    if config.get('LOG_CLEANUP_ENABLED', False):
        hours = int(config.get('LOG_CLEANUP_HOURS', 168))  # 默认保留7天=168小时
        interval_hours = int(config.get('LOG_CLEANUP_INTERVAL', 24))
        
        # 创建调度器
        scheduler = BackgroundScheduler()
        
        # 添加定时任务，每隔指定小时执行一次
        scheduler.add_job(
            cleanup_logs,
            'interval',
            hours=interval_hours,
            kwargs={'hours': hours},
            id='log_cleanup_job'
        )
        
        # 启动调度器
        scheduler.start()
        
        logger.info(f"已启用日志自动清理，保留{hours}小时内的日志，每{interval_hours}小时清理一次")
        return scheduler
    else:
        logger.info("日志自动清理已禁用")
        return None

@app.route('/maintenance/cleanup_logs', methods=['POST'])
def cleanup_logs_route():
    """手动触发日志清理"""
    config = load_config()
    hours = int(request.form.get('hours', config.get('LOG_CLEANUP_HOURS', 168)))
    
    result = cleanup_logs(hours)
    
    if result.get('success'):
        flash(f"日志清理成功，删除了{result['files_removed']}个文件，释放了{result['bytes_freed_readable']}空间", 'success')
    else:
        flash(f"日志清理失败: {result.get('error', '未知错误')}", 'danger')
    
    return redirect(url_for('settings'))

@app.route('/maintenance/clear_logs', methods=['POST'])
def clear_logs_route():
    """立即清空特定日志文件"""
    result = clear_specific_logs()
    
    if result.get('success'):
        processed_files_str = "、".join(result['processed_files'])
        flash(f"日志清理成功，已处理{result['files_processed']}个文件（{processed_files_str}），释放了{result['bytes_freed_readable']}空间", 'success')
    else:
        flash(f"日志清理失败: {result.get('error', '未知错误')}", 'danger')
    
    return redirect(url_for('settings'))

if __name__ == '__main__':
    logger.info("Y2A-Auto 启动中...")
    
    # 初始化AcFun分区ID映射
    init_id_mapping()
    
    # 加载配置
    config = load_config()
    app.config['Y2A_SETTINGS'] = config
    logger.info(f"配置已加载: {json.dumps(config, ensure_ascii=False, indent=2)}")
    
    # 配置应用
    configure_app(app, config)
    
    # 设置日志清理定时任务
    log_cleanup_scheduler = schedule_log_cleanup()
    
    try:
        logger.info(f"服务启动，监听地址: http://127.0.0.1:{5000}")
        # 使用标准Flask运行
        app.run(host='0.0.0.0', port=5000, debug=False)
    except KeyboardInterrupt:
        logger.info("接收到退出信号，服务正在关闭...")
    except Exception as e:
        logger.error(f"服务启动失败: {str(e)}")
    finally:
        if log_cleanup_scheduler:
            log_cleanup_scheduler.shutdown()
        logger.info("服务已关闭") 