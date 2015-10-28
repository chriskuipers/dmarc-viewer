from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from django.db.models import Q

from django.contrib.gis.db import models
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.db.models.fields.related import ForeignKey
from django.db.models import Sum, Count, Max
import choices
import copy

############################
"""
DMARC AGGREGATE REPORT MODEL
"""
############################

class Reporter(models.Model):
    org_name                = models.CharField(max_length = 100)
    email                   = models.EmailField()
    extra_contact_info      = models.CharField(max_length = 200, null = True)

    def __unicode__(self):
        return self.org_name

class Report(models.Model):
    """In the Schema a report is called feedback"""
    # Custom field to easily differ between incoming and outgoing
    report_type             = models.IntegerField(choices = choices.REPORT_TYPE)
    date_created            = models.DateTimeField(auto_now = False, auto_now_add = True)
    
    # Meta Data 
    report_id               = models.CharField(max_length = 200, unique = True)
    date_range_begin        = models.DateTimeField()
    date_range_end          = models.DateTimeField()

    version                 = models.DecimalField(max_digits = 4, decimal_places = 2, null = True)
    reporter                = models.ForeignKey('Reporter')

    # Policy Published
    domain                  = models.CharField(max_length = 100)
    adkim                   = models.IntegerField(choices = choices.ALIGNMENT_MODE, null = True)
    aspf                    = models.IntegerField(choices = choices.ALIGNMENT_MODE, null = True)
    p                       = models.IntegerField(choices = choices.DISPOSITION_TYPE)
    sp                      = models.IntegerField(choices = choices.DISPOSITION_TYPE, null = True)
    pct                     = models.IntegerField(null = True)
    fo                      = models.CharField(max_length = 8, null = True)

    @staticmethod
    def getOldestReportDate(report_type = choices.INCOMING):
        date_qs = Report.objects.order_by("date_range_begin")\
                    .filter(report_type=report_type)\
                    .values("date_range_begin").first()
        if date_qs:
            return date_qs["date_range_begin"]
        else:
            return None

    @staticmethod
    def getOverviewSummary(report_type = choices.INCOMING):
        return {
            "domain_cnt"  : Report.objects.filter(report_type=report_type).distinct("domain").count(),
            "report_cnt"  : Report.objects.filter(report_type=report_type).count(),
            "message_cnt" : Record.objects.filter(report__report_type=report_type).aggregate(cnt=Sum("count"))['cnt'],
            # Query per result aggregated message count for dkim, spf and dispostion
            # Transform result number to display name
            "dkim"        : [{"cnt": res["cnt"], "label": dict(choices.DMARC_RESULT).get(res["dkim"])}
                              for res in Record.objects.filter(report__report_type=report_type).values("dkim").annotate(cnt=Sum("count"))],
            "spf"         : [{"cnt": res["cnt"], "label": dict(choices.DMARC_RESULT).get(res["spf"])}
                              for res in Record.objects.filter(report__report_type=report_type).values("spf").annotate(cnt=Sum("count"))],
            "disposition" : [{"cnt": res["cnt"], "label": dict(choices.DISPOSITION_TYPE).get(res["disposition"])}
                              for res in Record.objects.filter(report__report_type=report_type).values("disposition").annotate(cnt=Sum("count"))],
        }

class ReportError(models.Model):
    report                  = models.ForeignKey('Report')
    error                   = models.CharField(max_length = 200)

class Record(models.Model):
    report                  = models.ForeignKey('Report')

    # Row
    source_ip               = models.GenericIPAddressField(null = True)
    country_iso_code        = models.CharField(max_length = 2)
    geometry                = models.PointField(srid=4326, null = True)
    objects                 = models.GeoManager()

    count                   = models.IntegerField()

    # Policy Evaluated
    disposition             = models.IntegerField(choices = choices.DISPOSITION_TYPE)
    dkim                    = models.IntegerField(choices = choices.DMARC_RESULT)
    spf                     = models.IntegerField(choices = choices.DMARC_RESULT)

    # Identifiers
    envelope_to             = models.CharField(max_length = 100, null = True)
    envelope_from           = models.CharField(max_length = 100, null = True)
    header_from             = models.CharField(max_length = 100, null = True)

    # Custom field for filter convenience (needs one join less)
    auth_result_dkim_count  = models.IntegerField(default=0)

class PolicyOverrideReason(models.Model):
    record                  = models.ForeignKey('Record')
    reason_type             = models.IntegerField(choices = choices.POLICY_REASON_TYPE, null = True)
    reason_comment          = models.CharField(max_length = 200, null = True)

class AuthResultDKIM(models.Model):
    record                  = models.ForeignKey('Record')
    domain                  = models.CharField(max_length = 100)
    result                  = models.IntegerField(choices = choices.DKIM_RESULT)
    human_result            = models.CharField(max_length = 200, null = True)

class AuthResultSPF(models.Model):
    record                  = models.ForeignKey('Record')
    domain                  = models.CharField(max_length = 100)
    scope                   = models.IntegerField(choices = choices.SPF_SCOPE, null = True)
    result                  = models.IntegerField(choices = choices.SPF_RESULT)

############################
"""
MYDMARC VIEW/FILTER MODEL

Notes:
- FilterFields that reference same Model Field are ORed
- FilterFields that reference different Model Fields are ANDed
"""
############################
#
class OrderedModel(models.Model):
    position                   = models.PositiveIntegerField(default = 0)

    def save(self):
        """If new assign order number (max(order of all objects) or 0)
        If exists save normal."""

        if not self.id:
            try:
                self.position = self.__class__.all().aggregate(Max("position")).value("price__max") + 1
            except Exception, e:
                self.position = 0 # Defaut anyways, do this for more explicitness
        super(OrderedModel, self).save()

    @staticmethod
    def order(orderedObjects):
        """Assign index as order and save to each object of ordered object list"""
        for idx, obj in enumerate(orderedObjects):
            obj.position = idx
            obj.save()

    class Meta:
        ordering = ["position"]
        abstract = True


class View(OrderedModel):
    title                   = models.CharField(max_length = 100)
    description             = models.TextField(null = True)
    enabled                 = models.BooleanField(default = True)
    type_map                = models.BooleanField(default = True)
    type_table              = models.BooleanField(default = True)
    type_line               = models.BooleanField(default = True)


    def getViewFilterFieldManagers(self):
        return _get_related_managers(self, ViewFilterField)

    def getTableRecords(self):

        # Combine all Filtersets
        query = reduce(lambda x, y: x | y, [fs.getQuery() for fs in self.filterset_set.all()])
        # Only retrieve records of a page
        return Record.objects.filter(query).distinct().order_by('report__date_range_begin') #[page_start:page_end]
        #use this for list comprehension
        # PROBLEM: can't assign filterset label or color if it is all combined


    def getTableData(self, records=None):
        """If records list or querymanager is specified, use it instead of
        records for this view. this can be useful for pagination"""

        return [[r.report.reporter.org_name,
                r.report.domain,
                r.source_ip,
                r.country_iso_code,
                r.report.date_range_begin.strftime('%Y%m%d'),
                r.report.date_range_end.strftime('%Y%m%d'),
                r.count,
                ' '.join([dkim.domain for dkim in r.authresultdkim_set.all()]),
                ' '.join([dkim.get_result_display() for dkim in r.authresultdkim_set.all()]),
                r.get_dkim_display(),
                ' '.join([spf.domain for spf in r.authresultspf_set.all()]),
                ' '.join([spf.get_result_display() for spf in r.authresultspf_set.all()]),
                r.get_spf_display(),
                r.get_disposition_display(),
                # fs.label] for fs in self.filterset_set.all() for r in fs.getRecords().distinct()]
                r.report.report_id
                ] for r in records or self.getTableRecords()]

    def getCsvData(self):
        csv_head = ["reporter", "domain", "ip", "country", 
                "date_range_begin", "date_range_end", 
                "count", "dkim domains", "dkim results", 
                "aligned dkim", "spf domains", "spf results",
                "aligned spf", "disposition"]

        return [csv_head] + self.getTableData()

    def getLineData(self):
        # There must only one of both exactly one 
        date_range = DateRange.objects.filter(foreign_key=self.id).first()
        if not date_range:
            raise Exception("You have to specify a date range, you bastard!") # XXX LP Raise proper exception
        begin, end = date_range.getBeginEnd()

        return {'begin': begin.strftime('%Y%m%d'), 
                'end': end.strftime('%Y%m%d'), 
                'data_sets': [{'label': filter_set.label,
                               'color': filter_set.color,
                               'data': list(filter_set.getMessageCountPerDay())} \
                                        for filter_set in self.filterset_set.all()]}

    def getMapData(self):
        return [{'label': filter_set.label,
                 'color': filter_set.color,
                 'data' : list(filter_set.getMessageCountPerCountry())} \
                                     for filter_set in self.filterset_set.all()]

class FilterSet(models.Model):
    view                    = models.ForeignKey('View')
    label                   = models.CharField(max_length = 100)
    color                   = models.CharField(max_length = 7)
    multiple_dkim           = models.NullBooleanField()

    def getQuery(self):
        # Get a list of object managers, each of which contains according filter field objects of one class
        filter_field_managers = [manager for manager in self.getFilterSetFilterFieldManagers()] + \
            [manager for manager in self.view.getViewFilterFieldManagers()]

        #All filter fields of same class are ORed
        or_queries = []
        for manager in filter_field_managers:
            filter_fields = manager.all()
            if filter_fields:
                or_queries.append(reduce(lambda x, y: x | y, [filter_field.getRecordFilter() for filter_field in filter_fields]))

        # All filter fields of different classes are ANDed
        if or_queries:
            return reduce(lambda x, y: x & y, [or_query for or_query in or_queries])
        else:
            return Q()

    def getRecords(self):
        query = self.getQuery()
        return Record.objects.filter(query)

    def getMessageCountPerDay(self):
        # XXX LP: to_char is postgres specific, do we care for db flexibility?
        # XXX L: I don't like to hardcode APP Label
        return self.getRecords()\
                .extra(select={'date': "to_char(\"myDmarcApp_report\".\"date_range_begin\", 'YYYYMMDD')"})\
                .values('date')\
                .annotate(cnt=Sum('count'))\
                .values('date', 'cnt')\
                .order_by('date')
    
    def getMessageCountPerCountry(self):
        return self.getRecords()\
                .values('country_iso_code')\
                .annotate(cnt=Sum('count'))\
                .values('country_iso_code', 'cnt')

    def getFilterSetFilterFieldManagers(self):
        return _get_related_managers(self, FilterSetFilterField)


class FilterSetFilterField(models.Model):
    foreign_key             = models.ForeignKey('FilterSet')

    def getRecordFilter(self):
        key = self.record_field.replace('.', "__").lower()
        return Q(**{key: self.value})

    class Meta:
        abstract = True

class ViewFilterField(models.Model):
    foreign_key             = models.ForeignKey('View')
    class Meta:
        abstract = True

class ReportType(ViewFilterField):
    value             = models.IntegerField(choices = choices.REPORT_TYPE)
    def getRecordFilter(self):
        return Q(**{"report__report_type": self.value})

class DateRange(ViewFilterField):
    """
    Either DATE_RANGE_TYPE_FIXED or DATE_RANGE_TYPE_VARIABLE
    """
    dr_type      = models.IntegerField(choices = choices.DATE_RANGE_TYPE)
    begin        = models.DateTimeField(null = True)
    end          = models.DateTimeField(null = True)
    unit         = models.IntegerField(choices = choices.TIME_UNIT, null = True)
    quantity     = models.IntegerField(null = True)

    def getBeginEnd(self):
        if (self.dr_type == choices.DATE_RANGE_TYPE_FIXED):
            return self.begin, self.end
        elif (self.dr_type == choices.DATE_RANGE_TYPE_VARIABLE):
            end = datetime.now()
            if (self.unit == choices.TIME_UNIT_DAY):
                begin = end - relativedelta(days=self.quantity)
            elif (self.unit == choices.TIME_UNIT_WEEK):
                begin = end - relativedelta(weeks=self.quantity)
            elif (self.unit == choices.TIME_UNIT_MONTH):
                begin = end - relativedelta(months=self.quantity)
            elif (self.unit == choices.TIME_UNIT_YEAR):
                begin = end - relativedelta(years=self.quantity)
            else:
                raise # XXX LP proper Exception
            return begin, end        
        else:
            raise # XXX LP proper Exception

    def getRecordFilter(self):
        begin, end = self.getBeginEnd()
        return Q(**{"report__date_range_begin__gte" : begin}) & Q(**{"report__date_range_begin__lte": end})

    def __str__(self):
        return "%s - %s" % (self.getBeginEnd())


class ReportSender(FilterSetFilterField):
    record_field            = "Report.Reporter.email"
    value                   = models.CharField(max_length = 100)

class ReportReceiverDomain(FilterSetFilterField):
    label                   = "Report Sender Domain"
    record_field            = "Report.domain"
    value                   = models.CharField(max_length = 100)

class SourceIP(FilterSetFilterField):
    """let's start with simple IP address filtering 
    and maybe consider CIDR notation later"""
    record_field            = "source_ip"
    value                   = models.GenericIPAddressField()

class RawDkimDomain(FilterSetFilterField):
    record_field            = "AuthResultDKIM.domain"
    value                   = models.CharField(max_length = 100)

class RawDkimResult(FilterSetFilterField):
    record_field            = "AuthResultDKIM.result"
    value                   = models.IntegerField(choices = choices.DKIM_RESULT)

class MultipleDkim(FilterSetFilterField):
    value                   = models.BooleanField(default = False)
    def getRecordFilter(self):
        return Q(**{"auth_result_dkim_count__gt" : 1})

class RawSpfDomain(FilterSetFilterField):
    record_field            = "AuthResultSPF.domain"
    value                   = models.CharField(max_length = 100)

class RawSpfResult(FilterSetFilterField):
    record_field            = "AuthResultSPF.result"
    value                   = models.IntegerField(choices = choices.SPF_RESULT)

class AlignedDkimResult(FilterSetFilterField):
    record_field            = "dkim"
    value                   = models.IntegerField(choices = choices.DMARC_RESULT)

class AlignedSpfResult(FilterSetFilterField):
    record_field            = "spf"
    value                   = models.IntegerField(choices = choices.DMARC_RESULT)

class Disposition(FilterSetFilterField):
    record_field            = "disposition"
    value                   = models.IntegerField(choices = choices.DISPOSITION_TYPE)


def _get_related_managers(obj, parent_class=False):
    # XXX LP _get_fields is a rather internal django function,
    # not sure if I should use it here
    foreign_object_relations = obj._meta._get_fields(False)
    foreign_managers = []

    for rel in foreign_object_relations:
        # Check for parent class if wanted
        if parent_class and not issubclass(rel.related_model, parent_class):
            continue
        foreign_managers.append(getattr(obj, rel.get_accessor_name()))
    return foreign_managers

def _get_related_objects(obj, parent_class=False):
    """Helper method to get an object's foreign key related objects.
        Satisfying polymorphism workaround. Get related objects of a FilterSet.
        Alternatives might be:
            The contenttypes framework (too complicated)
            django_polymorphic (based on above, tried but did not work as expected)

    Params:
        parent_class (default false)
            related object's class must be (a subclass) of type parent_class
    Returns:
        list of related objects
        
    XXX LP maybe use chain from itertools for better performance

    """
    foreign_managers = _get_related_managers(obj, parent_class)
    # Get objects
    related_objects = []
    for manager in foreign_managers:
       related_objects += manager.all()

    return related_objects

def _clone(obj, parent_obj = False):
    """recursivly clone an object and its related objects"""

    related_objects = _get_related_objects(obj)
    obj.pk = None

    # If we got a parent_object, obj is already related
    # lets assign parent_object's id as foreign key
    if parent_obj:
        for field in obj._meta.fields:
            if isinstance(field, ForeignKey) and isinstance(parent_obj, field.related_model):
                setattr(obj, field.name, parent_obj)
    # saving without pk, will auomatically create new record
    obj.save()

    for related_object in related_objects:
        _clone(related_object, obj)