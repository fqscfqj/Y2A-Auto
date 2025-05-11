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
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_cors import CORS, cross_origin
from flask_socketio import SocketIO, emit
from modules.youtube_handler import download_video_data
from modules.utils import parse_id_md_to_json, process_cover
from modules.config_manager import load_config, update_config, DEFAULT_CONFIG
from modules.task_manager import add_task, start_task, get_task, get_all_tasks, get_tasks_by_status, update_task, delete_task, force_upload_task, TASK_STATES, clear_all_tasks
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = os.urandom(24)  # 用于flash消息
app.jinja_env.globals.update(now=datetime.now())  # 添加当前时间到模板全局变量

# 配置SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

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
file_handler.setLevel(logging.INFO)

# 控制台处理器
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)
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
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# 强制设置所有日志记录器的默认编码为UTF-8
logging.getLogger().handlers[0].encoding = 'utf-8'
if len(logging.getLogger().handlers) > 1:
    logging.getLogger().handlers[1].encoding = 'utf-8'

# 配置应用日志记录器
logger = logging.getLogger('Y2A-Auto')
logger.setLevel(logging.WARNING)

# 确保静态目录存在
covers_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'covers')
os.makedirs(covers_dir, exist_ok=True)

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
        TASK_STATES['TRANSLATING']: '翻译中',
        TASK_STATES['TAGGING']: '生成标签中',
        TASK_STATES['PARTITIONING']: '推荐分区中',
        TASK_STATES['MODERATING']: '内容审核中',
        TASK_STATES['AWAITING_REVIEW']: '等待人工审核',
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
        TASK_STATES['TRANSLATING']: 'info',
        TASK_STATES['TAGGING']: 'info',
        TASK_STATES['PARTITIONING']: 'info',
        TASK_STATES['MODERATING']: 'info',
        TASK_STATES['AWAITING_REVIEW']: 'warning',
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

# WebSocket实时通知函数
def notify_task_completion(task_id, success=True, ac_number=None, title=None):
    """通过WebSocket发送任务完成通知"""
    try:
        if ac_number:
            acfun_video_url = f"https://www.acfun.cn/v/ac{ac_number}"
            socketio.emit('task_completed', {
                'task_id': task_id,
                'success': success,
                'ac_number': ac_number,
                'title': title,
                'url': acfun_video_url
            })
        else:
            socketio.emit('task_completed', {
                'task_id': task_id,
                'success': success
            })
        logger.info(f"已通过WebSocket发送任务 {task_id} 完成通知")
    except Exception as e:
        logger.error(f"发送WebSocket通知失败: {str(e)}")

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
    
    # 确保封面图片可访问
    for task in review_tasks:
        if task.get('cover_path_local'):
            # 复制封面图片到静态目录
            cover_path = task['cover_path_local']
            static_cover_path = os.path.join(covers_dir, f"{task['id']}.jpg")
            
            if os.path.exists(cover_path) and not os.path.exists(static_cover_path):
                try:
                    shutil.copy2(cover_path, static_cover_path)
                except Exception as e:
                    logger.error(f"复制封面图片失败: {str(e)}")
    
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
        
        # 执行上传操作（当任务处于"已完成"或"等待处理"状态时）
        task = get_task(task_id)  # 重新获取更新后的任务信息
        if task['status'] in [TASK_STATES['COMPLETED'], TASK_STATES['PENDING']]:
            # 获取当前配置
            config = load_config()
            
            # 尝试执行上传
            logger.info(f"开始上传任务 {task_id} 到AcFun")
            flash('正在上传到AcFun，请等待...', 'info')
            
            try:
                # 调用上传函数
                success = force_upload_task(task_id, config)
                
                if success:
                    flash('上传成功！您的视频已成功上传到AcFun', 'success')
                    logger.info(f"任务 {task_id} 上传成功")
                else:
                    flash('上传失败，请查看日志了解详情', 'danger')
                    logger.error(f"任务 {task_id} 上传失败")
            except Exception as e:
                flash(f'上传过程中发生错误: {str(e)}', 'danger')
                logger.error(f"任务 {task_id} 上传出错: {str(e)}")
                import traceback
                logger.error(traceback.format_exc())
        else:
            flash('任务已保存，但尚未完成处理，无法上传', 'warning')
        
        return redirect(url_for('tasks'))
    
    # GET请求，显示编辑页面
    # 确保封面图片可访问
    if task.get('cover_path_local'):
        cover_path = task['cover_path_local']
        static_cover_path = os.path.join(covers_dir, f"{task_id}.jpg")
        
        if os.path.exists(cover_path) and not os.path.exists(static_cover_path):
            try:
                shutil.copy2(cover_path, static_cover_path)
            except Exception as e:
                logger.error(f"复制封面图片失败: {str(e)}")
    
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
            
            def check_task_completion():
                import time
                # 等待30秒
                time.sleep(30)
                # 检查任务状态
                completed_task = get_task(task_id)
                if completed_task and completed_task['status'] == TASK_STATES['COMPLETED'] and completed_task.get('acfun_upload_response'):
                    try:
                        # 解析上传响应
                        upload_response = json.loads(completed_task['acfun_upload_response'])
                        ac_number = upload_response.get('ac_number')
                        title = upload_response.get('title', '未知标题')
                        if ac_number:
                            logger.info(f"任务 {task_id} 已完成，AC号: {ac_number}")
                            # 发送WebSocket通知
                            notify_task_completion(task_id, True, ac_number, title)
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.error(f"解析上传响应失败: {str(e)}")
            
            # 启动后台线程检查任务完成情况
            threading.Thread(target=check_task_completion).start()
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
    
    # 强制上传
    success = force_upload_task(task_id, config)
    
    if success:
        # 获取最新的任务信息，包含上传结果
        updated_task = get_task(task_id)
        if updated_task and updated_task.get('acfun_upload_response'):
            try:
                # 解析上传响应
                upload_response = json.loads(updated_task['acfun_upload_response'])
                ac_number = upload_response.get('ac_number')
                title = upload_response.get('title', '未知标题')
                
                if ac_number:
                    # 构建AcFun视频链接
                    acfun_video_url = f"https://www.acfun.cn/v/ac{ac_number}"
                    flash(f'视频《{title}》上传成功！AC号: {ac_number} <a href="{acfun_video_url}" target="_blank">点击查看</a>', 'success')
                    notify_task_completion(task_id, True, ac_number, title)
                else:
                    flash(f'视频《{title}》上传成功，但未获取到AC号', 'success')
                    notify_task_completion(task_id, True)
            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"解析上传响应失败: {str(e)}")
                flash('任务已上传成功，但无法获取上传详情', 'warning')
                notify_task_completion(task_id, True)
        else:
            # 如果没有上传响应，也认为是成功的强制上传（可能之前已上传或无需上传）
            flash('任务已强制上传（可能之前已处理或无需上传响应）', 'success')
            notify_task_completion(task_id, True)
    else:
        flash('强制上传失败', 'danger')
        notify_task_completion(task_id, False)
    
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
        if 'youtube_cookies_file' in request.files:
            cookies_file = request.files['youtube_cookies_file']
            if cookies_file.filename:
                # 确保config目录存在
                config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config')
                os.makedirs(config_dir, exist_ok=True)
                
                # 获取配置中的cookies路径文件名（仅文件名部分）
                cookies_filename = os.path.basename(form_data.get('YOUTUBE_COOKIES_PATH', 'cookies.txt'))
                
                # 构建config目录下的完整路径
                config_cookies_path = os.path.join(config_dir, cookies_filename)
                
                # 保存文件到config目录
                cookies_file.save(config_cookies_path)
                
                # 更新配置中的路径，指向config目录下的文件
                form_data['YOUTUBE_COOKIES_PATH'] = os.path.join('config', cookies_filename)
                
                logger.info(f"YouTube cookies文件已上传并保存到: {config_cookies_path}")
        
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
                # 处理封面
                process_cover(task['cover_path_local'], mode=mode)
                
                # 更新静态目录封面
                static_cover_path = os.path.join(covers_dir, f"{task_id}.jpg")
                if os.path.exists(static_cover_path):
                    os.remove(static_cover_path)
                
                shutil.copy2(task['cover_path_local'], static_cover_path)
        
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

# 背景任务监控
@socketio.on('connect')
def handle_connect():
    """处理WebSocket连接"""
    logger.info(f"客户端连接到WebSocket: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    """处理WebSocket断开连接"""
    logger.info(f"客户端断开WebSocket连接: {request.sid}")

# 监控特定任务的状态
@socketio.on('monitor_task')
def handle_monitor_task(data):
    """监控特定任务的状态"""
    task_id = data.get('task_id')
    if task_id:
        logger.info(f"客户端 {request.sid} 开始监控任务 {task_id}")
        # 注册任务监控
        # 这里不需要实际操作，因为我们会在任务完成时广播给所有客户端
        emit('monitor_started', {'task_id': task_id})

# 日志清理功能
def cleanup_logs(days=7):
    """
    清理指定天数以前的日志文件
    
    Args:
        days: 保留最近多少天的日志
    
    Returns:
        cleanup_stats: 清理统计信息
    """
    try:
        logger.info(f"开始清理{days}天前的日志文件")
        cutoff_date = datetime.now() - timedelta(days=days)
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

def schedule_log_cleanup():
    """根据配置设置日志清理定时任务"""
    config = load_config()
    
    if config.get('LOG_CLEANUP_ENABLED', False):
        days = int(config.get('LOG_CLEANUP_DAYS', 7))
        interval_hours = int(config.get('LOG_CLEANUP_INTERVAL', 24))
        
        # 创建调度器
        scheduler = BackgroundScheduler()
        
        # 添加定时任务，每隔指定小时执行一次
        scheduler.add_job(
            cleanup_logs,
            'interval',
            hours=interval_hours,
            kwargs={'days': days},
            id='log_cleanup_job'
        )
        
        # 启动调度器
        scheduler.start()
        
        logger.info(f"已启用日志自动清理，保留{days}天内的日志，每{interval_hours}小时清理一次")
        return scheduler
    else:
        logger.info("日志自动清理已禁用")
        return None

@app.route('/maintenance/cleanup_logs', methods=['POST'])
def cleanup_logs_route():
    """手动触发日志清理"""
    config = load_config()
    days = int(request.form.get('days', config.get('LOG_CLEANUP_DAYS', 7)))
    
    result = cleanup_logs(days)
    
    if result.get('success'):
        flash(f"日志清理成功，删除了{result['files_removed']}个文件，释放了{result['bytes_freed_readable']}空间", 'success')
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
        socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        logger.info("接收到退出信号，服务正在关闭...")
    except Exception as e:
        logger.error(f"服务启动失败: {str(e)}")
    finally:
        if log_cleanup_scheduler:
            log_cleanup_scheduler.shutdown()
        logger.info("服务已关闭") 