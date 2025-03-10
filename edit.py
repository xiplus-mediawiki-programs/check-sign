#!/usr/bin/env python
# coding: utf-8

import argparse
import bisect
import configparser
import html
import json
import os
import re
import time
from datetime import datetime, timedelta

import pymysql

BASEDIR = os.path.dirname(os.path.realpath(__file__))
os.environ['PYWIKIBOT_DIR'] = BASEDIR
import pywikibot
import pywikibot.flow
import requests
from dbmodels import User
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from config import CONFIG_PAGE_NAME, REPLICA_CONFIG_PATH, USERDB_CONFIG_PATH

QUERY_USER_WITH_SIGN = '''
SELECT actor_user, actor_name, up2.up_value AS `nickname`
FROM recentchanges
LEFT JOIN actor ON rc_actor = actor_id
LEFT JOIN user_properties AS up1 ON actor_user = up1.up_user AND up1.up_property = 'fancysig'
LEFT JOIN user_properties AS up2 ON actor_user = up2.up_user AND up2.up_property = 'nickname'
WHERE (rc_namespace = 4 OR rc_namespace % 2 = 1)
    AND rc_timestamp > DATE_FORMAT(DATE_SUB(NOW(), INTERVAL 7 DAY), '%Y%m%d%H%i%s')
    AND actor_user IS NOT NULL
    AND up1.up_value IS NOT NULL
    AND up2.up_value IS NOT NULL
GROUP BY actor_user
ORDER BY rc_timestamp DESC
'''

OUTPUT_HEADER = '''{| class="wikitable sortable"
!使用者
!檢查
!簽名
!問題
'''

OUTPUT_ROW = '''|-
| [[Special:Contributions/{0}|{0}]]
| {1}
| {2}
| {3}
'''


def nomalize_name(name):
    name = name.replace('_', ' ').strip()
    name = name[0].upper() + name[1:]
    return name


def check_sign_problems(sign, username):
    sign_errors = set()
    hide_sign = False

    if '[[File:' in sign:
        sign_errors.add((2, 'file', None))
    if '<div' in sign:
        sign_errors.add((2, 'div', None))
        hide_sign = True
    if len(re.findall(r'{{', sign)) > len(re.findall(r'{{!}}', sign)):
        sign_errors.add((2, 'template', None))
    if '<templatestyles' in sign:
        sign_errors.add((2, 'templatestyles', None))
    if re.search(r'\[(https?)?://', sign):
        sign_errors.add((2, 'link', None))
    sign_len = len(sign.encode())
    if sign_len > 255:
        sign_errors.add((1, 'sign-too-long', sign_len))

    names_in_sign = set()
    sign = html.unescape(sign)
    for name in re.findall(r'\[\[\s*:?(?:User(?:[ _]talk)?|U|UT|用户|用戶|使用者|用戶對話|用戶討論|用户对话|用户讨论|使用者討論)\s*:\s*([^\]/|#]+?)\s*[\]|#]', sign, re.I):
        names_in_sign.add(nomalize_name(name))
    for name in re.findall(r'\[\[\s*:?(?:Special|特殊)\s*:(?:(?:Contributions|Contribs)|(?:用户|用戶|使用者)?(?:贡献|貢獻))/(?:\s*User:\s*)?([^\]/|#]+?)\s*[\]/|#]', sign, re.I):
        names_in_sign.add(nomalize_name(name))
    other_names_in_sign = names_in_sign - {username}
    if len(names_in_sign) > 1 and not re.search(r'bot$', username):
        sign_errors.add((3, 'ambiguous', '、'.join(sorted(other_names_in_sign))))
    elif len(names_in_sign) == 0:
        sign_errors.add((3, 'nolink', None))
    elif username not in names_in_sign:
        sign_errors.add((3, 'otherlink', '、'.join(sorted(other_names_in_sign))))

    return sign_errors, hide_sign


def format_sign_errors_output(sign_errors):
    result = []
    sign_errors = sorted(list(sign_errors))

    for row in sign_errors:
        error_type = row[1]
        error_param = row[2]

        if error_type == 'file':
            result.append('檔案')
        elif error_type == 'template':
            result.append('模板')
        elif error_type == 'templatestyles':
            result.append('模板樣式')
        elif error_type == 'link':
            result.append('外部連結')
        elif error_type == 'sign-too-long':
            result.append('簽名過長-{}'.format(error_param))
        elif error_type == 'ambiguous':
            result.append('混淆-<nowiki>{}</nowiki>'.format(error_param))
        elif error_type == 'nolink':
            result.append('無連結')
        elif error_type == 'otherlink':
            result.append('偽造-<nowiki>{}</nowiki>'.format(error_param))
        elif error_type == 'obsolete-tag':
            result.append('過時的標籤-{}'.format(error_param))
        else:
            if error_param:
                result.append('{}-{}'.format(error_type, error_param))
            else:
                result.append(error_type)

    return '、'.join(result)


def get_warn_templates(sign_errors, username):
    templates = set()
    for row in sign_errors:
        error_type = row[1]
        error_param = row[2]

        if error_type == 'file':
            templates.add('Uw-sign-file')
        elif error_type in ['template', 'templatestyles']:
            templates.add('Uw-sign-notemplate')
        elif error_type == 'link':
            templates.add('Uw-sign-external-link')
        elif error_type == 'sign-too-long':
            templates.add('Uw-sign-toolong')
        elif error_type == 'ambiguous' and not re.search(r'bot$', username):
            templates.add('Uw-sign-link-ambiguous|1=' + error_param)
        elif error_type == 'nolink':
            templates.add('Uw-signlink')
        elif error_type == 'otherlink':
            templates.add('Uw-sign-link-mismatch')
    return templates


def format_sign_errors_report(sign_errors):
    result = []
    sign_errors = sorted(list(sign_errors))

    for row in sign_errors:
        error_type = row[1]
        error_param = row[2]

        if error_type == 'file':
            result.append('[[Wikipedia:签名#外观|包含檔案]]')
        elif error_type == 'template':
            result.append('[[Wikipedia:签名#外部链接与模板|包含模板]]')
        elif error_type == 'templatestyles':
            result.append('[[Wikipedia:签名#外部链接与模板|包含模板樣式]]')
        elif error_type == 'link':
            result.append('[[Wikipedia:签名#外部链接与模板|包含外部連結]]')
        elif error_type == 'sign-too-long':
            result.append('[[Wikipedia:签名#长度|簽名過長（{}位元組）]]'.format(error_param))
        elif error_type == 'ambiguous':
            result.append('[[Wikipedia:签名#假冒签名|假冒簽名（簽名連結到其他人的用戶頁、討論頁或貢獻頁）]]')
        elif error_type == 'nolink':
            result.append('[[Wikipedia:签名#签名必须包含的部分|簽名未連結到用戶頁、討論頁或貢獻頁]]')
        elif error_type == 'otherlink':
            result.append('[[Wikipedia:签名#假冒签名|假冒簽名（簽名連結到其他人的用戶頁、討論頁或貢獻頁）]]')

    return '、'.join(result)


def parse_signs(signs):
    text = ''
    for userid, sign in signs.items():
        text += '<!-- {0} start -->{1}<!-- {0} end -->\n'.format(userid, sign)

    with open(os.path.join(BASEDIR, 'raw_signs.txt'), 'w', encoding='utf8') as f:
        f.write(text)

    API = 'https://zh.wikipedia.org/w/api.php'
    data = requests.post(API, data={
        'action': 'parse',
        'format': 'json',
        'text': text,
        'onlypst': 1,
        'contentmodel': 'wikitext',
    }).json()
    parsed_text = data['parse']['text']['*']

    parsed_signs = {}
    for userid in signs:
        flag = '<!-- {} start -->'.format(userid)
        idx1 = parsed_text.index(flag)
        idx2 = parsed_text.index('<!-- {} end -->'.format(userid))
        parsed_sign = parsed_text[idx1 + len(flag):idx2]
        parsed_signs[userid] = parsed_sign

    return parsed_signs


def lint_signs(signs):
    text = ''
    signs_idx = []
    userids = []
    sign_errors = {}
    for userid, sign in signs.items():
        cleaned_sign = re.sub(r'[^\x00-\x7F]', 'C', sign)
        text += '<div id="sign-{}">{}</div>\n'.format(userid, cleaned_sign)
        signs_idx.append(len(text))
        userids.append(userid)
        sign_errors[userid] = set()

    with open(os.path.join(BASEDIR, 'parsed_signs.txt'), 'w', encoding='utf8') as f:
        f.write(text)

    data = requests.post('https://zh.wikipedia.org/api/rest_v1/transform/wikitext/to/lint', data=json.dumps({
        'wikitext': text,
    }).encode(), headers={
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }).json()

    with open(os.path.join(BASEDIR, 'lint.json'), 'w', encoding='utf8') as f:
        json.dump(data, f, indent=4)

    temp = []
    for row in data:
        idx = bisect.bisect_left(signs_idx, row['dsr'][0])
        userid = userids[idx]
        lint_type = row['type']
        if lint_type == 'night-mode-unaware-background-color':
            lint_tag = None
        else:
            lint_tag = row['params']['name']
        sign_errors[userid].add((3, lint_type, lint_tag))
        temp.append([userids[idx], text[row['dsr'][0]:row['dsr'][1]], row['dsr'], lint_type, lint_tag])

    with open(os.path.join(BASEDIR, 'lint-res.json'), 'w', encoding='utf8') as f:
        json.dump(temp, f, indent=4)

    return sign_errors


reader = configparser.ConfigParser()
reader.read(USERDB_CONFIG_PATH)
USERDB_HOST = reader.get('client', 'host')
USERDB_PORT = reader.get('client', 'port')

engine = create_engine(
    f'mysql+pymysql://{USERDB_HOST}:{USERDB_PORT}',
    connect_args={'read_default_file': USERDB_CONFIG_PATH},
)


def get_sign_url(username):
    return 'https://signatures.toolforge.org/check/zh.wikipedia.org/{}'.format(username.replace(' ', '%20'))


def warn_user(site, username, sign, sign_error, warn_templates, cfg, args):
    print('warn', username, warn_templates)
    if args.no_warn:
        return
    TALK_NAMESPACES = list(filter(lambda v: v >= 0 and v % 2 == 1 or v == 4, site.namespaces))
    TIMELIMIT = datetime.now() - timedelta(days=7)

    session = Session(engine)
    user = session.query(User).filter(User.name == username).first()
    if user is None:
        user = User(
            name=username,
        )
        session.add(user)
        session.commit()

    if user.last_warn > TIMELIMIT:
        print('\trecent warned')
        return

    contributions = site.usercontribs(
        user=username,
        total=500,
        namespaces=TALK_NAMESPACES,
        end=TIMELIMIT,
    )
    has_recent_edit = False
    for contris in contributions:
        print('\t', contris)
        page = pywikibot.Page(site, contris['title'])
        old_text = page.getOldVersion(contris['parentid']) if contris['parentid'] > 0 else ''
        new_text = page.getOldVersion(contris['revid'])
        if old_text is None:
            print('\trev {} is deleted'.format(contris['parentid']))
            continue
        if new_text is None:
            print('\trev {} is deleted'.format(contris['revid']))
            continue
        old_cnt = old_text.count(sign)
        new_cnt = new_text.count(sign)
        if new_cnt > old_cnt:
            print('\trecent edit', contris['revid'], old_cnt, new_cnt)
            has_recent_edit = True
            break
    if not has_recent_edit:
        print('\tno recent edit')
        return

    if user.warn_count >= 3:
        report_page = pywikibot.Page(site, cfg['report_page'])
        new_text = report_page.text

        report_flag = '<!-- sign report: {} -->'.format(username)

        if report_flag in new_text:
            print('\treported')
            return

        report_text = '\n=== {} ===\n'.format(username)
        report_text += "* '''{{{{vandal|{}}}}}'''\n".format(('1=' if '=' in username else '') + username)
        report_text += '* 其[{} 簽名]違反簽名指引：{}，已警告3次仍未改善。{}\n'.format(
            get_sign_url(username),
            format_sign_errors_report(sign_error),
            report_flag
        )
        report_text += '* 提報人：~~~~\n'
        report_text += '* 处理：\n'

        new_text = re.sub('(\n===)', report_text + r'\1', new_text, 1)
        if args.confirm:
            pywikibot.showDiff(report_page.text, new_text)
            input('Save?')
        report_page.text = new_text
        report_page.save(summary=cfg['report_summary'], minor=False)
    else:
        user.warn_count += 1
        title = '簽名問題'
        if user.warn_count > 1:
            title += '（第{}次）'.format(user.warn_count)

        talk_page = pywikibot.Page(site, 'User talk:' + username)
        if talk_page.is_flow_page():
            board = pywikibot.flow.Board(talk_page)
            content = ''
            for template in warn_templates:
                content += '{{subst:' + template + '}}\n\n'
            content += cfg['notice_suffix']
            if args.confirm:
                print('\tflow {}: {}'.format(title, content))
                input('Save?')
            board.new_topic(title, content)
        else:
            new_text = talk_page.text
            if new_text != '':
                new_text += '\n\n'
            new_text += '== {} ==\n'.format(title)
            for template in warn_templates:
                new_text += '{{subst:' + template + '}}\n\n'
            new_text += cfg['notice_suffix'] + '--~~~~'
            if args.confirm:
                pywikibot.showDiff(talk_page.text, new_text)
                input('Save?')
            talk_page.text = new_text
            talk_page.save(summary=cfg['notice_summary'], minor=False)

    user.last_warn = datetime.now()
    session.commit()


def main(args):
    site = pywikibot.Site('zh', 'wikipedia')
    site.login()

    config_page = pywikibot.Page(site, CONFIG_PAGE_NAME)
    cfg = config_page.text
    cfg = json.loads(cfg)
    if args.confirm:
        print(json.dumps(cfg, indent=4, ensure_ascii=False))

    if not cfg['enable']:
        print('disabled')
        exit()

    conn = pymysql.connect(read_default_file=REPLICA_CONFIG_PATH)

    with conn.cursor() as cur:
        cur.execute(QUERY_USER_WITH_SIGN)
        res = cur.fetchall()

    user_ids = []
    raw_signs = {}
    usernames = {}
    for row in res:
        userid = row[0]
        username = row[1].decode()
        sign = row[2].decode()

        if re.search(r'^(Former|Renamed|Vanished|Deleted) (user|account) ', username, flags=re.I):
            continue

        user_ids.append(userid)
        raw_signs[userid] = sign
        usernames[userid] = username

    print('Process {} users'.format(len(user_ids)))

    parsed_signs = parse_signs(raw_signs)

    sign_errors = {}
    hide_sign = {}
    for userid, sign in parsed_signs.items():
        sign_errors[userid], hide_sign[userid] = check_sign_problems(sign, usernames[userid])

    lint_sign_errors = lint_signs(parsed_signs)
    for userid, errors in lint_sign_errors.items():
        sign_errors[userid].update(errors)

    output_text = OUTPUT_HEADER
    warned_users = set()
    legal_users = set()
    for userid in sorted(usernames):
        error = sign_errors[userid]
        if len(error) > 0:
            check_link = '[{} check]'.format(get_sign_url(usernames[userid]))
            sign = ''
            if not hide_sign[userid]:
                sign = parsed_signs[userid]
            error_text = format_sign_errors_output(error)
            output_text += OUTPUT_ROW.format(usernames[userid], check_link, sign, error_text)

            warn_templates = get_warn_templates(error, usernames[userid])
            if len(warn_templates) > 0:
                warned_users.add(userid)
                warn_user(
                    site=site,
                    username=usernames[userid],
                    sign=parsed_signs[userid],
                    sign_error=error,
                    warn_templates=warn_templates,
                    cfg=cfg,
                    args=args,
                )
        else:
            legal_users.add(userid)

    output_text += '|}'

    page = pywikibot.Page(site, cfg['output_page'])

    if page.text != output_text:
        if args.confirm:
            print('Diff:')
            pywikibot.showDiff(page.text, output_text)
            print('-' * 50)

        page.text = output_text
        page.save(summary=cfg['output_summary'], minor=False)
    else:
        print('No diff')

    session = Session(engine)
    for user in session.query(User).all():
        if user.name in legal_users:
            print('Remove {} from table'.format(user.name))
            session.delete(user)
    session.commit()


if __name__ == '__main__':
    print(time.ctime())
    parser = argparse.ArgumentParser()
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--no-warn', action='store_true')
    parser.set_defaults(confirm=False, no_warn=False)
    args = parser.parse_args()

    main(args)
