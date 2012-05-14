import os
import time
import logging
try:
    import ujson as json
except ImportError:
    import json

from django.http import HttpResponse, Http404, HttpResponseRedirect
from django.core.exceptions import ObjectDoesNotExist
from django.shortcuts import render
from django.db.models import Q
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator

from models import Gallery, Image, Video, Tag

from common import MainView, Result, JsonResponse, getObjectsFromGuids
from uploader import uploader

from sendFile import send_file, send_zipfile

from dev.settings import MEDIA_ROOT


gRange = 300
logger = logging.getLogger('dev.frog')
LoginRequired = method_decorator(login_required)


class GalleryView(MainView):
    def __init__(self):
        super(GalleryView, self).__init__(Gallery)

    def index1(self, request):
        return render(request, 'frog/index.html', {})

    @LoginRequired
    def get(self, request, obj_id=None):
        if obj_id:
            return super(GalleryView, self).get(request, obj_id)
        else:
            res = Result()
            res.isSuccess = True
            for n in Gallery.objects.all():
                res.append(n.json())

            return JsonResponse(res)

    @LoginRequired
    def post(self, request):
        title = request.POST.get('title', 'New Gallery' + str(Gallery.objects.all().values_list('id', flat=True)[0] + 1))
        #parent = request.POST.get('parent', 1)
        g, created = Gallery.objects.get_or_create(title=title)

        res = Result()
        res.isSuccess = True
        res.append(g.json())
        res.message = 'Gallery created' if created else ''

        return JsonResponse(res)

    @LoginRequired
    def put(self, request, obj_id=None):
        guids = self.PUT.get('guids', '').split(',')
        objects = getObjectsFromGuids(guids)

        for o in objects:
            if isinstance(o, Image):
                self.object.images.add(o)
            elif isinstance(o, Video):
                self.object.videos.add(o)

        res = Result()
        res.isSuccess = True

        return JsonResponse(res)

    @LoginRequired
    def delete(self, request, obj_id=None):
        guids = self.DELETE.get('guids', '').split(',')
        objects = getObjectsFromGuids(guids)

        for o in objects:
            if isinstance(o, Image):
                self.object.images.remove(o)
            elif isinstance(o, Video):
                self.object.videos.remove(o)

        res = Result()
        res.isSuccess = True

        return JsonResponse(res)

    @LoginRequired
    def filter(self, request, obj_id):
        self._processRequest(request, obj_id)
        
        tags = json.loads(request.GET.get('filters', '[]'))
        rng = request.GET.get('rng', None)
        more = request.GET.get('more', False)
        models = request.GET.get('models', 'image,video')

        models = [ContentType.objects.get(app_label='frog', model=x) for x in models.split(',')]

        return self._filter(tags=tags, rng=rng, models=models, more=more)

    def _filter(self, tags=None, models=(Image, Video), rng=None, more=False):
        ''' filter on
        search, tags, models
        accept range in #:#
        return dict
        '''

        NOW = time.clock()

        res = Result()
        
        idDict = {}
        objDict = {}
        lastIDs = {}

        logger.debug('init: %f' % (time.clock() - NOW))

        self.request.session.setdefault('frog_range', '0:%i' % gRange)

        if rng:
            s, e = [int(x) for x in rng.split(':')]
        else:
            if more:
                s = int(self.request.session.get('frog_range', '0:%i' % gRange).split(':')[1])
                e = s + gRange
                s, e = 0, gRange
            else:
                s, e = 0, gRange

        logger.info(self.request.session['frog_range'])

        for m in models:
            lastIndex = m.model_class().objects.all().values_list('id', flat=True)[0]
            if more:
                self.request.session.setdefault('last_%s_id' % m.model, lastIndex + 1)
            else:
                self.request.session['last_%s_id' % m.model] = lastIndex + 1

            offset = lastIndex - self.request.session['last_%s_id' % m.model] + 1
            
            idDict[m.model] = m.model_class().objects.filter(gallery=self.object, id__lt=self.request.session['last_%s_id' % m.model])
            logger.debug(m.model + '_initial_query: %f' % (time.clock() - NOW))

            if tags:
                for bucket in tags:#[t for t in tags if isinstance(t, int) or isinstance(t, long)]:
                    o = Q()
                    for item in bucket:
                        if isinstance(item, int) or isinstance(item, long):
                            o |= Q(tags__id=item)
                        else:
                            ## -- Too simple, replace with solution using a real search engine. overkill?
                            o |= Q(title__icontains=item)
                    idDict[m.model] = idDict[m.model].filter(o)
                logger.debug(m.model + '_added_buckets(%i): %f' % (len(tags), time.clock() - NOW))
            else:
                # all
                pass
            
            idDict[m.model] = idDict[m.model].values_list('id', flat=True)
            logger.debug(m.model + '_queried_ids: %f' % (time.clock() - NOW))

            res.message = str(s) + ':' + str(e)
            
            objDict[m.model] = m.model_class().objects.filter(id__in=idDict[m.model]).select_related('author').prefetch_related('tags')
            if not rng:
                objDict[m.model] = objDict[m.model][:gRange]
            objDict[m.model] = list(objDict[m.model])
            logger.debug(m.model + '_queried_obj: %f' % (time.clock() - NOW))
        
        objects = self._sortObjects(**objDict) if len(models) > 1 else objDict.values()[0]
        objects = objects[s:e]
        logger.debug('sorted: %f' % (time.clock() - NOW))

        for i in objects:
            for m in models:
                if isinstance(i, m.model_class()):
                    self.request.session['last_%s_id' % m.model] = i.id
            res.append(i.json())
        logger.debug('serialized: %f' % (time.clock() - NOW))

        self.request.session['frog_range'] = ':'.join((str(s),str(e)))

        logger.debug('total: %f' % (time.clock() - NOW))
        data = {
            'count': len(objects),
            'last_image_id': self.request.session.get('last_image_id', 0),
            'last_video_id': self.request.session.get('last_video_id', 0),
            'queries': connection.queries,
        }

        res.value = data

        res.isSuccess = True

        return JsonResponse(res)

    def _sortObjects(self, **args):
        o = []
        
        for m in args.values():
            for l in iter(m):
                o.append(l)
        o = list(set(o))
        o.sort(self._sortByCreated)

        return o

    def _sortByCreated(self, a,b):
        if a.created < b.created:
            return 1
        elif a.created > b.created:
            return -1
        else:
            if a.id < b.id:
                return 1
            elif a.id > b.id:
                return -1
            else:
                return 0


class TagView(MainView):
    def __init__(self):
        super(TagView, self).__init__(Tag)

    @LoginRequired
    def get(self, request, obj_id=None):
        if obj_id:
            return super(TagView, self).get(request, obj_id)
        else:
            res = Result()
            res.isSuccess = True
            for n in Tag.objects.all():
                res.append(n.json())

            return JsonResponse(res)

    @LoginRequired
    def post(self, request, obj_id=None):
        res = Result()
        if not obj_id:
            name = request.POST.get('name', None)

            if not name:
                res.isError = True
                res.message = "No name given"

                return JsonResponse(res)
            
            tag, created = Tag.objects.get_or_create(name=name)

            res.isSuccess = True
            if created:
                res.message = "Created"

            res.append(tag.json())

            return JsonResponse(res)

    @LoginRequired
    def put(self, request, obj_id=None):
        tagList = filter(None, request.POST.get('tags', '').split(','))
        guids = request.POST.get('guids', '').split(',')
        res = Result()
        res.isSuccess = True

        self._manageTags(tagList, guids)

        return JsonResponse(res)

    @LoginRequired
    def delete(self, request, obj_id=None):
        tagList = filter(None, self.DELETE.get('tags', '').split(','))
        guids = self.DELETE.get('guids', '').split(',')
        res = Result()
        res.isSuccess = True

        self._manageTags(tagList, guids, add=False)

        return JsonResponse(res)

    @LoginRequired
    def search(self, request):
        q = request.GET.get('q', '')
        includeSearch = request.GET.get('search', False)
        nonZero = request.GET.get('zero', False)
        excludeArtist = request.GET.get('artist', False)

        if includeSearch:
            l = [{'id': 0, 'name': 'Search for: %s' % q}]
        else:
            l = []

        query = Tag.objects.filter(name__icontains=q)

        if excludeArtist:
            query = query.exclude(artist=True)

        if nonZero:
            l += [t.json() for t in query if t.count() > 0]
        else:
            l += [t.json() for t in query]

        return JsonResponse(l)

    @LoginRequired
    def manage(self, request):
        if request.method == 'GET':
            guids = request.GET.get('guids', '').split(',')
            guids = filter(None, guids)

            objects = getObjectsFromGuids(guids)
            ids = [o.id for o in objects]

            tags = list(set(Tag.objects.filter(image__id__in=ids).exclude(artist=True)))

            if request.GET.get('json', False):
                res = Result()
                data = {
                    'queries': connection.queries,
                }

                res.value = data

                res.isSuccess = True

                return JsonResponse(res)

            return render(request, 'frog/tag_manage.html', {'tags': tags})
        else:
            add = request.POST.get('add', '').split(',')
            rem = request.POST.get('rem', '').split(',')
            guids = request.POST.get('guids', '').split(',')

            add = filter(None, add)
            rem = filter(None, rem)

            objects = getObjectsFromGuids(guids)
            addTags = Tag.objects.filter(id__in=add)
            remTags = Tag.objects.filter(id__in=rem)

            for o in objects:
                for a in addTags:
                    o.tags.add(a)
                for r in remTags:
                    o.tags.remove(r)

            res = Result()
            res.isSuccess = True

            return JsonResponse(res)

    def _manageTags(self, tagList, guids, add=True):
        objects = getObjectsFromGuids(guids)
        tags = []
        for tag in tagList:
            try:
                t = Tag.objects.get(pk=int(tag))
            except ValueError:
                t, created = Tag.objects.get_or_create(name=tag)
            tags.append(t)

        if add:
            return self._addTags(tags, objects)
        else:
            return self._removeTags(tags, objects)

    def _addTags(self, tags, objects):
        for t in tags:
            for o in objects:
                o.tags.add(t)

        return True

    def _removeTags(selfl, tags, objects):
        for t in tags:
            for o in objects:
                o.tags.remove(t)

        return True



class ImageView(MainView):
    def __init__(self, *args):
        super(ImageView, self).__init__(Image)

    @LoginRequired
    def post(self, request, obj_id):
        tags = request.POST.get('tags', '').split(',')
        res = Result()
        for tag in tags:
            try:
                t = Tag.objects.get(pk=int(tag))
            except ValueError:
                t, created = Tag.objects.get_or_create(name=tag)
                if created:
                    res.append(t.json())
            self.object.tags.add(t)

        res.isSuccess = True

        return JsonResponse(res)

    @LoginRequired
    def delete(self, request, obj_id):
        self.object.deleted = True
        self.object.save()
        res = Result()
        res.isSuccess = True
        res.value = self.object.json()
        return JsonResponse(res)


class VideoView(ImageView):
    def __init__(self):
        super(VideoView, self).__init__(Video)

@login_required
def downloadView(request):
    guids = request.GET.get('guids', '').split(',')

    if guids:
        objects = getObjectsFromGuids(guids)
        if len(objects) == 1:
            response = send_file(request, MEDIA_ROOT + objects[0].source.name)
            response['Content-Disposition'] = 'attachment; filename=%s' % os.path.split(objects[0].foreign_path)[1]
            return response
        else:
            fileList = {}
            for n in objects:
                fileList.setdefault(n.author.username, [])
                fileList[n.author.username].append([MEDIA_ROOT + n.source.name, os.path.split(n.foreign_path)[1]])

            response = send_zipfile(request, fileList)
            return response

def index(request):
    if request.method == 'GET':
        if not request.user.is_anonymous():
            return HttpResponseRedirect('/frog/gallery/1')
        return render(request, 'frog/index.html', {'title': 'Frog Login'})
    else:
        return uploader.post(request)

def frogLogin(request):
    res = Result()

    res.isSuccess = True
    email = request.POST.get('email', 'noauthor@domain.com')
    username = email.split('@')[0]
    first_name = request.POST.get('first_name', 'No')
    last_name = request.POST.get('last_name', 'Author')

    user = authenticate(username=username)
    user.first_name = first_name
    user.last_name = last_name
    user.email = email
    user.save()

    Tag.objects.get_or_create(name=first_name + ' ' + last_name, defaults={'artist': True})

    if user.is_active:
        login(request, user)
        return HttpResponseRedirect('/frog/gallery/1')
    else:
        return render(request, 'frog/index.html', {'message': 'User account not active'})

def frogLogout(request):
    logout(request)

    return HttpResponseRedirect('/frog')


gallery = GalleryView()
tag = TagView()
image = ImageView()
video = VideoView()