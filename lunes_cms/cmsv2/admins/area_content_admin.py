from __future__ import absolute_import, annotations, unicode_literals

from typing import Any, TYPE_CHECKING

from django.contrib import admin
from django.contrib.admin.options import IS_POPUP_VAR
from django.contrib.admin.widgets import RelatedFieldWidgetWrapper
from django.http import HttpRequest

from ..areas import accessible_areas, administered_areas, default_area
from .base import BaseAdmin

if TYPE_CHECKING:
    from django.db.models import Field, ForeignKey, Model
    from django.forms import ModelChoiceField, ModelForm
    from django.forms.models import BaseInlineFormSet

    from ..models import Area

#: The query parameter that tells an add popup the area of the job, unit or
#: word it was opened from, see :class:`AreaContentInline`
POPUP_AREA_VAR = "area"


class PopupAreaWidgetWrapper(RelatedFieldWidgetWrapper):
    """
    A related widget that passes the area of the object it is shown on to its
    add popup, see :class:`AreaContentInline`.
    """

    def __init__(self, wrapper: RelatedFieldWidgetWrapper, area_id: int) -> None:
        super().__init__(
            wrapper.widget,
            wrapper.rel,
            wrapper.admin_site,
            can_add_related=wrapper.can_add_related,
            can_change_related=wrapper.can_change_related,
            can_delete_related=wrapper.can_delete_related,
            can_view_related=wrapper.can_view_related,
        )
        self.area_id = area_id

    def get_context(
        self, name: str, value: Any, attrs: dict[str, Any] | None
    ) -> dict[str, Any]:
        context = super().get_context(name, value, attrs)
        context["url_params"] += f"&{POPUP_AREA_VAR}={self.area_id}"
        return context


class AreaContentInline(admin.TabularInline):
    """
    Inline linking content of an area to a job, unit or word.

    Linked content has to share the area of the object it is linked to. The
    "+" icon of :attr:`area_content_field` creates new content in a popup,
    which saves it right away, before the relation is validated along with
    the page. So the popup is told the area to put the content into, see
    :meth:`AreaContentAdmin.popup_area`.
    """

    #: The field whose add popup creates the linked content
    area_content_field: str

    def get_formset(
        self, request: HttpRequest, obj: Any = None, **kwargs: Any
    ) -> type["BaseInlineFormSet[Any, Any, Any]"]:
        formset = super().get_formset(request, obj, **kwargs)
        field = formset.form.base_fields.get(self.area_content_field)
        # The area of an object that is still being added is only picked on
        # the page, so its popup keeps letting the area be chosen.
        if (
            obj is not None
            and field is not None
            and isinstance(field.widget, RelatedFieldWidgetWrapper)
        ):
            field.widget = PopupAreaWidgetWrapper(field.widget, obj.area_id)
        return formset


class AreaContentAdmin(BaseAdmin):
    """
    Base admin class for content that belongs to an area: jobs, units and
    words.

    It decides who may pick the area of the content and which area new
    content is put into, see :func:`~lunes_cms.cmsv2.areas.default_area`.
    """

    def popup_area(self, request: HttpRequest) -> Area | None:
        """
        The area of the job, unit or word an add popup was opened from, see
        :class:`AreaContentInline`.

        The content added in such a popup is about to be linked to that
        object, so it has to be put into its area.
        """
        area_id = request.GET.get(POPUP_AREA_VAR, "")
        if IS_POPUP_VAR not in request.GET or not area_id.isdigit():
            return None
        return accessible_areas(request.user).filter(pk=area_id).first()

    def get_readonly_fields(
        self, request: HttpRequest, obj: Model | None = None
    ) -> list[str]:
        """
        Only superusers may move content between areas, and content added in
        a popup stays in the area it is about to be linked into, see
        :meth:`popup_area`.

        An area administrator who administers several areas still has to pick
        one when creating content, so the field stays editable on the add form —
        limited to their own areas, see :meth:`formfield_for_foreignkey`.
        """
        readonly_fields = list(super().get_readonly_fields(request, obj))
        if obj is None and self.popup_area(request):
            return [*readonly_fields, "area"]
        if request.user.is_superuser:
            return readonly_fields
        if obj is None and len(administered_areas(request.user)) > 1:
            return readonly_fields
        return [*readonly_fields, "area"]

    def get_form(
        self,
        request: HttpRequest,
        obj: Model | None = None,
        change: bool = False,
        **kwargs: Any,
    ) -> type["ModelForm[Any]"]:
        """
        Preselect the default area of the user on the add form.

        The form takes the initial value of the field from its instance, so
        the fresh instance of the add form is created with the default area
        right away. That covers the editable field as well as the read-only
        one (see :meth:`get_readonly_fields`), which only ever displays the
        instance's own ``area``.
        """
        form = super().get_form(request, obj, change, **kwargs)
        if obj is not None:
            return form
        area = self.popup_area(request) or default_area(request.user)

        class PreselectedAreaForm(form):  # type: ignore[misc,valid-type]
            """``form``, with a fresh instance already pointed at the area."""

            def __init__(self, *args: Any, **inner_kwargs: Any) -> None:
                if inner_kwargs.get("instance") is None:
                    inner_kwargs["instance"] = self._meta.model(area=area)
                super().__init__(*args, **inner_kwargs)

        return PreselectedAreaForm

    def formfield_for_foreignkey(
        self,
        db_field: "ForeignKey[Any, Any]",
        request: HttpRequest,
        **kwargs: Any,
    ) -> "ModelChoiceField[Any] | None":
        """
        Offer an area administrator only the areas they administer.

        The area cannot be emptied, so content created by an area
        administrator never ends up in the wrong area by accident.
        """
        if db_field.name == "area" and not request.user.is_superuser:
            areas = administered_areas(request.user)
            if areas.exists():
                kwargs["queryset"] = areas
                kwargs["required"] = True
                kwargs["empty_label"] = None
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_dbfield(
        self, db_field: "Field[Any, Any]", request: HttpRequest, **kwargs: Any
    ) -> Any:
        """
        Switch off the add/change/delete/view-related icons of the area field.

        ``formfield_for_foreignkey`` builds the plain field; the base
        implementation of this method is what wraps it with those icons
        afterwards, so it is only here, once that wrapping has happened,
        that they can be turned back off. Managing an entire area from a
        content edit form is a shortcut nobody needs here, and a confusing (in
        the delete case, dangerous) one to leave lying around; the area
        change and list pages are one click away regardless.
        """
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        widget = getattr(formfield, "widget", None)
        if db_field.name == "area" and isinstance(widget, RelatedFieldWidgetWrapper):
            widget.can_add_related = False
            widget.can_change_related = False
            widget.can_delete_related = False
            widget.can_view_related = False
        return formfield

    def save_model(
        self,
        request: HttpRequest,
        obj: Any,
        form: "ModelForm[Model]",
        change: bool,
    ) -> None:
        """
        Put new content of a single-area administrator into their area.

        Administrators of a single area never see the field — it is
        read-only for them, see :meth:`get_readonly_fields` — so their area
        never reaches the form. It is filled in here, the same way
        ``created_by`` is, in case the instance did not come from
        :meth:`get_form`.
        """
        if not change and not request.user.is_superuser:
            areas = list(administered_areas(request.user))
            if len(areas) == 1:
                obj.area = areas[0]
        super().save_model(request, obj, form, change)
