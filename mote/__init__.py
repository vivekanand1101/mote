# -*- coding: utf-8 -*-
#
# Copyright © 2015 Chaoyi Zha <cydrobolt@fedoraproject.org>
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions
# of the GNU General Public License v.2, or (at your option) any later
# version.  This program is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY expressed or implied, including the
# implied warranties of MERCHANTABILITY or FITNESS FOR A PARTICULAR
# PURPOSE.  See the GNU General Public License for more details.  You
# should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#

import os

try:
    # if different config directory provided
    # e.g ran in mod_wsgi
    import site
    config_path = os.environ['MOTE_CONFIG_FOLDER']
    site.addsitedir(config_path) # default: "/etc/mote"
except:
    # different config directory not specified
    # e.g running from git clone
    pass

import flask, random, string, json, util, re
import dateutil.parser, requests, collections, arrow
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, url_for, session, redirect
from flask_fas_openid import fas_login_required, cla_plus_one_required, FAS
from util import RegexConverter

fn_search_regex = "(.*?)\.([0-9]{4}\-[0-9]{2}\-[0-9]{2})\-.*?\..*?\.(.*)"

import config

__version__ = "0.0.0"

user_sessions = dict()

app = Flask("mote")
fas = FAS(app)
app.secret_key = ''.join(random.SystemRandom().choice(string.uppercase + string.digits) for _ in xrange(20))
app.config['FAS_OPENID_ENDPOINT'] = 'http://id.fedoraproject.org/'
app.config['FAS_CHECK_CERT'] = True
cwd = os.getcwd()
app.url_map.converters['regex'] = RegexConverter

if config.use_mappings_github == True:
    name_mappings = requests.get("https://raw.githubusercontent.com/fedora-infra/mote/master/name_mappings.json").text
    category_mappings = requests.get("https://raw.githubusercontent.com/fedora-infra/mote/master/category_mappings.json").text
else:
    with open(config.name_mappings_path, 'r') as f:
        name_mappings = f.read()
    with open(config.category_mappings_path, 'r') as f:
        category_mappings = f.read()

name_mappings = json.loads(name_mappings)
category_mappings = json.loads(category_mappings)

if config.use_memcached == True:
    import memcache
    mc = memcache.Client([config.memcached_ip], debug=0)

def return_error(msg):
    return render_template('error.html', error=msg)

def get_cache_data(key_name):
    if key_name == "mote:team_meetings":
        meeting_type = "team"
    elif key_name == "mote:channel_meetings":
        meeting_type = "channel"
    else:
        meeting_type = None

    if config.use_memcached == True:
        try:
            res = mc.get(key_name)
            if res == None:
                raise ValueError('Could not find requested key.')
            return res
        except ValueError:
            try:
                res = util.get_json_cache(meeting_type)
            except RuntimeError:
                soke.run()
                res = util.get_json_cache(meeting_type)
            return res
    else:
        # Skip memcached, use JSON store directly
        try:
            res = util.get_json_cache(meeting_type)
        except RuntimeError:
            soke.run()
            res = util.get_json_cache(meeting_type)
        return res


@app.route('/', methods=['GET'])
def index():
    # Renders main page template.
    return render_template('index.html')

@app.route('/post_auth', methods=['GET'])
@app.route('/post_auth/', methods=['GET'])
@fas_login_required
def post_auth():
    # Set local session variables after
    # FedOAuth authenticates the user.
    session['logged'] = True
    return redirect(url_for('index'))

@app.route('/<meeting_channel>/<regex("([0-9]{4}\-[0-9]{2}\-[0-9]{2})"):date_stamp>')
@app.route('/<meeting_channel>/<regex("([0-9]{4}\-[0-9]{2}\-[0-9]{2})"):date_stamp>/')
def catch_channel_daterequest(meeting_channel, date_stamp):
    try:
        meetings = get_cache_data("mote:channel_meetings")
        workable_array = meetings[meeting_channel][date_stamp]
        minutes = workable_array["minutes"]
        logs = workable_array["logs"]
        return render_template(
            "date-list.html",
            minutes=minutes,
            date=date_stamp,
            logs=logs,
            type="channel",
            group_name=meeting_channel
        )
    except Exception as inst:
        print inst
        return return_error("Requested meetings could not be located.")

@app.route('/teams/<meeting_team>/<regex("([0-9]{4}\-[0-9]{2}\-[0-9]{2})"):date_stamp>')
@app.route('/teams/<meeting_team>/<regex("([0-9]{4}\-[0-9]{2}\-[0-9]{2})"):date_stamp>/')
def catch_team_daterequest(meeting_team, date_stamp):
    try:
        meetings = get_cache_data("mote:team_meetings")
        workable_array = meetings[meeting_team][date_stamp]
        minutes = workable_array["minutes"]
        logs = workable_array["logs"]
        return render_template(
            "date-list.html",
            date=date_stamp,
            minutes=minutes,
            logs=logs,
            type="team",
            group_name=meeting_team
        )
    except:
        return return_error("Requested meetings could not be located.")


@app.route('/teams/<meeting_team>')
@app.route('/teams/<meeting_team>/')
def catch_team_baserequest(meeting_team):
    url = flask.url_for('sresults', group_id=meeting_team, type='team')
    return flask.redirect(url)


@app.route('/<meeting_channel>/<date>/<regex("(.*?)\.[0-9]{4}\-[0-9]{2}\-[0-9]{2}\-.*"):file_name>')
def catch_channel_logrequest(date, file_name, meeting_channel):
    # This route catches standard log requests.
    # Links referencing a meeting channel will be caught by this route.
    # These URLs include those provided by MeetBot at the end of a meeting,
    # or links referencing a specific meeting channel,
    # such as #fedora-meeting or #fedora-ambassadors
    log_gtype = "channel"
    m = re.search(fn_search_regex, file_name)
    group_name = m.group(1) # name of channel, e.g fedora-meeting
    meeting_date = date # date of log requested: YYYY-MM-DD
    log_extension = m.group(3) # type of log requested: log.html, html, or txt
    log_type = util.get_meeting_type(log_extension)
    if group_name != meeting_channel:
        # Prefer using team names if one can be extracted from the filename.
        log_gtype = "team"
    if log_type == "plain-text":
        # Redirect to the plaintext file is one is requested.
        built_url = "{}/{}/{}/{}".format(config.meetbot_prefix, meeting_channel, date, file_name)
        return redirect(built_url)

    return render_template("single-log.html", gtype=log_gtype, ltype=log_type, group=group_name, date=meeting_date, filename=file_name)

@app.route('/teams/<meeting_team>/<regex("(.*?)\.[0-9]{4}\-[0-9]{2}\-[0-9]{2}\-.*"):file_name>')
def catch_team_logrequest(file_name, meeting_team):
    # This route catches standard log requests.
    # Links referencing a meeting team will be caught by this route.
    # e.g referencing famna or infrastructure

    m = re.search(fn_search_regex, file_name)
    group_name = m.group(1) # name of team, e.g famna
    meeting_date = m.group(2) # date of log requested: YYYY-MM-DD
    log_extension = m.group(3) # type of log requested: log.html, html, or txt
    log_type = util.get_meeting_type(log_extension)
    if log_type == "plain-text":
        built_url = "{}/teams/{}/{}".format(config.meetbot_prefix, meeting_team, file_name)
        return redirect(built_url)
    return render_template("single-log.html", gtype="team", ltype=log_type, group=group_name, date=meeting_date, filename=file_name)

@app.route('/request_logs')
@app.route('/request_logs/')
def request_logs():
    """ Return a list of filenames for minutes and/or logs
    for a specified date.
    """
    group_id = request.args["group_id"]
    group_type = request.args["group_type"]
    date_stamp = request.args["date_stamp"]
    if group_type == "team":
        meetings = get_cache_data("mote:team_meetings")
    elif group_type == "channel":
        meetings = get_cache_data("mote:channel_meetings")
    try:
        workable_array = meetings[group_id][date_stamp]
        minutes = workable_array["minutes"]
        logs = workable_array["logs"]

        response = json.dumps({"minutes": minutes, "logs": logs})
        return response
    except:
        flask.abort(404)

@app.route('/get_meeting_log')
@app.route('/get_meeting_log/')
def get_meeting_log():
    """ Return specific logs or minutes to client. """
    group_type = request.args['group_type']
    date_stamp = request.args['date_stamp']
    group_id   = request.args['group_id']
    file_name  = request.args['file_name']
    file_type  = request.args.get('file_type')

    if group_type == "team":
        link_prefix_ending = "/teams/" + group_id + "/"
    else:
        link_prefix_ending = "/" + group_id + "/" + date_stamp + "/"

    url = config.meetbot_fetch_prefix + link_prefix_ending + file_name

    try:
        fetch_result = requests.get(url)
        fetch_soup = BeautifulSoup(fetch_result.text)
        if file_type == "log":
            # text = re.compile('your regex here')
            full_log_links = fetch_soup.findAll('a', text="full logs")
            for a in full_log_links:
                # prefix "full logs" links with correct paths
                full_log_file_name = a['href']
                a['href'] = link_prefix_ending + full_log_file_name
                a['target'] = "_blank"

        body_content = str(fetch_soup.body)
        body_content = body_content.replace("</br>", "")
        return body_content
    except Exception as e:
        print e
        flask.abort(404)

@app.route('/sresults', methods=['GET'])
@app.route('/sresults/', methods=['GET'])
def sresults():
    # Display results for a meeting group.
    group_id = request.args.get('group_id', '')
    group_type = request.args.get('type', '')
    try:
        friendly_name = name_mappings[group_id]["friendly-name"]
    except:
        friendly_name = False
    if (group_id == '') or (group_type == ''):
        return return_error("Invalid group ID or type.")

    if group_type == "team":
        meetings = get_cache_data("mote:team_meetings")
    elif group_type == "channel":
        meetings = get_cache_data("mote:channel_meetings")
    else:
        return return_error("Invalid group type.")
    try:
        groupx_meetings = meetings[group_id]
    except:
        return return_error("Group not found.")

    sorted_dates = list(groupx_meetings.keys())
    try:
        sorted_dates.sort(key=dateutil.parser.parse)
    except:
        return return_error("An error occured while fetching meetings.")

    avail_dates = collections.OrderedDict()

    try:
        for date in sorted_dates:
            parsed_date = dateutil.parser.parse(date)
            month = parsed_date.strftime("%B")
            year = parsed_date.year
            if year not in avail_dates:
                avail_dates[year] = collections.OrderedDict()
            if month not in avail_dates[year]:
                avail_dates[year][month] = []
            avail_dates[year][month].append(date)
        sorted_date_items = avail_dates.items()
        sorted_date_items.reverse()
        avail_dates = collections.OrderedDict(sorted_date_items)
    except:
        pass
    return render_template('sresults.html',
        friendly_name = friendly_name,
        name = group_id,
        type = group_type,
        avail_dates = avail_dates,
        meetbot_location = config.meetbot_prefix,
        latest_meeting = next(reversed(avail_dates.items()[0][1].items()[-1][1]))
    )


@app.route('/search_sugg', methods=['GET'])
@app.route('/search_sugg/', methods=['GET'])
def search_sugg():
    # Find and return the top 20 search results.
    search_term = request.args.get('q', '')
    channel_meetings = get_cache_data("mote:channel_meetings")
    team_meetings = get_cache_data("mote:team_meetings")
    results = []
    res_num = 0
    display_num = 20
    for cmk in channel_meetings:
        if res_num >= display_num:
            break
        if search_term in cmk:
            try:
                friendly_name = name_mappings[cmk]["friendly-name"]
            except:
                friendly_name = "A friendly meeting group."

            dates = [arrow.get(date) for date in channel_meetings[cmk].keys()]
            if not dates:
                continue
            dates.sort()
            latest = dates.pop()

            results.append({
                "id": cmk,
                "name": cmk,
                "type": "channel",
                "description": friendly_name,
                "latest": latest.timestamp,
                "latest_human": latest.humanize(),
            })
            res_num += 1

    for tmk in team_meetings:
        if res_num >= display_num:
            break
        if search_term in tmk:
            try:
                friendly_name = name_mappings[tmk]["friendly-name"]
            except:
                friendly_name = "A friendly meeting group."

            dates = [arrow.get(date) for date in team_meetings[tmk].keys()]
            if not dates:
                continue
            dates.sort()
            latest = dates.pop()

            results.append({
                "id": tmk,
                "name": tmk,
                "type": "team",
                "description": friendly_name,
                "latest": latest.timestamp,
                "latest_human": latest.humanize(),
            })
            res_num += 1

    # Sort results based on relevance.
    results = list(reversed(sorted(results, key=lambda k: k['latest'])))
    return flask.jsonify(dict(items=results))


@app.route('/auth', methods=['GET'])
@app.route('/auth/', methods=['GET'])
def auth_login():
    groups = config.admin_groups
    next_url = url_for('post_auth')
    return fas.login(return_url=next_url, groups=groups)

@app.route('/logout', methods=['GET'])
@app.route('/logout/', methods=['GET'])
def logout():
    if flask.g.fas_user:
        fas.logout()
        session['logged'] = None
    return redirect(url_for('index'))

@app.route('/admin', methods=['GET'])
@app.route('/admin/', methods=['GET'])
@fas_login_required
def admin_panel():
    is_admin = False
    for admin_group in config.admin_groups:
        if admin_group in flask.g.fas_user.groups:
            is_admin = True
    if is_admin == True:
        return render_template("admin.html")
    else:
        return render_template('error.html', error="Your account does not have access to this resource.")

@app.route('/browse', methods=['GET'])
@app.route('/browse/', methods=['GET'])
def browse():
    browse_nmappings = dict()
    for category_index in category_mappings:
        for category in category_mappings[category_index]:
            try:
                browse_nmappings[category] = name_mappings[category]["friendly-name"]
            except:
                browse_nmappings[category] = category
    return render_template('browse.html', category_mappings=category_mappings, browse_nmappings=browse_nmappings)
