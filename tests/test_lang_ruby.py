"""Ruby: Rails config/routes.rb (resources, namespaces, scopes, member/collection, verb routes, Devise), Sinatra and
Grape routes, require_relative and Rails autoload imports, externals, and handler call flows. Wren Tea is invented."""
from __future__ import annotations

from pathlib import Path

import pytest

import code_scan
from kestrel_fixtures import write
from langs import ruby, rubytext

R = "wren-shop"

WREN = {
    f"{R}/Gemfile": "source 'https://rubygems.org'\ngem 'rails', '~> 7.1'\ngem 'pg'\ngem 'redis'\ngem 'aws-sdk-sqs'\ngem 'aws-sdk-sesv2'\n",
    f"{R}/config/routes.rb": '''Rails.application.routes.draw do
  # get 'commented/out' => 'nope#nope'
  root 'home#index'
  devise_for :users, controllers: { sessions: :sessions },
                     path_names: { sign_in: :login }

  namespace :api do
    resources :teas, param: :slug, except: [:new, :edit] do
      member do
        post :steep
      end
      collection do
        get 'seasonal'
      end
      resources :reviews, only: [:index, :create, :destroy]
      get :pairings, on: :collection
    end
    resource :basket, only: [:show, :update]
    scope path: 'v2', module: 'v2' do
      get 'blends/:id' => 'blends#show'
      post 'blends', to: 'blends#create'
    end
  end

  scope :admin do
    match 'reports', to: 'reports#index', via: [:get, :post]
    if Rails.env.development?
      get 'debug', to: 'debug#show'
    end
  end
end
''',
    f"{R}/app/controllers/application_controller.rb": "class ApplicationController < ActionController::API\nend\n",
    f"{R}/app/controllers/api/teas_controller.rb": '''module Api
  class TeasController < ApplicationController
    def index
      @teas = Tea.where(active: true).order(:name)
      @teas = @teas.limit(params[:limit]) if params[:limit].present?
      render json: @teas
    end

    def show
      @tea = Tea.find_by!(slug: params[:slug])
    end

    # Brew a pot: reserve leaves, queue the brew, email the customer.
    def steep
      tea = load_tea
      if tea.nil?
        return head :not_found
      end
      tea.update!(steeping: true)
      begin
        BrewQueue.new.enqueue(tea)
      rescue Aws::SQS::Errors::ServiceError => e
        $redis.set("steep:failed:#{tea.id}", e.message)
      end
      render json: { ok: "done #{tea.name} end" }
    end

    def create
      tea = Tea.create!(tea_params)
      current_user.reviews.create(tea: tea, stars: 5) unless tea.blend?
      render json: tea, status: :created
    end

    private

    def load_tea
      Tea.find_by(slug: params[:slug])
    end

    def tea_params
      params.require(:tea).permit(:name)
    end
  end
end
''',
    f"{R}/app/controllers/api/reviews_controller.rb": '''module Api
  class ReviewsController < ApplicationController
    def index
      render json: Review.where(tea_id: params[:tea_slug])
    end

    def create
      Review.create!(stars: params[:stars])
    end

    def destroy
      Review.find(params[:id]).destroy
    end
  end
end
''',
    f"{R}/app/controllers/sessions_controller.rb": '''class SessionsController < ApplicationController
  def create
    user = User.find_by(email: params[:email])
    Rails.cache.write("session:#{user.id}", true)
  end
end
''',
    f"{R}/app/models/tea.rb": '''class Tea < ApplicationRecord
  has_many :reviews
  def blend?
    kind == 'blend'
  end
end
''',
    f"{R}/app/models/review.rb": "class Review < ApplicationRecord\n  belongs_to :tea\nend\n",
    f"{R}/app/models/user.rb": "class User < ApplicationRecord\n  has_many :reviews\nend\n",
    f"{R}/app/services/brew_queue.rb": '''require 'aws-sdk-sqs'
require_relative '../../lib/brew_mailer'

class BrewQueue
  def enqueue(tea)
    sqs = Aws::SQS::Client.new
    sqs.send_message(queue_url: ENV['BREW_QUEUE'], message_body: tea.id.to_s)
    BrewMailer.new.notify(tea)
  end
end
''',
    f"{R}/lib/brew_mailer.rb": '''require 'aws-sdk-sesv2'

class BrewMailer
  def notify(tea)
    Aws::SESV2::Client.new.send_email(destination: {})
  end
end
''',
    f"{R}/spec/requests/teas_spec.rb": "get '/spec-only' do\nend\n",
    f"{R}/vendor/bundle/junk.rb": "get '/vendored' do\nend\n",
    "kettle/Gemfile": "source 'https://rubygems.org'\ngem 'sinatra'\ngem 'ruby-kafka'\ngem 'sqlite3'\n",
    "kettle/app.rb": '''require 'sinatra/base'
require 'sinatra/namespace'
require 'kafka'

class Kettle < Sinatra::Base
  register Sinatra::Namespace

  KAFKA = Kafka.new(['localhost:9092'])

  get '/health' do
    'ok'
  end

  namespace '/kettles' do
    post '/:id/boil' do
      x = 1 if params[:id]
      KAFKA.deliver_message('boiled', topic: 'kettle-events')
      'boiling'
    end

    delete '/:id' do
      halt 204
    end
  end
end
''',
    "leafapi/Gemfile": "source 'https://rubygems.org'\ngem 'grape'\ngem 'mysql2'\ngem 'activerecord'\n",
    "leafapi/api/leaves.rb": '''module Leaf
  class API < Grape::API
    prefix :api
    version 'v1', using: :path
    format :json

    resource :leaves do
      desc 'All the leaves'
      get do
        Leaf.all
      end

      route_param :id do
        get do
          Leaf.find(params[:id])
        end
        put ':grade' do
          Leaf.find(params[:id]).update(grade: params[:grade])
        end
      end
    end
  end
end
''',
    "leafapi/models/leaf.rb": "class Leaf < ActiveRecord::Base\nend\n",
}


@pytest.fixture()
def cm(tmp_path: Path):
    return code_scan.scan([write(tmp_path, WREN)])


def _routes(cm) -> set[tuple[str, str]]:
    return {(r["m"], r["p"]) for r in cm.routes}


def _files(cm) -> list[str]:
    return [f["p"] for f in cm.files]


def test_rails_resources_namespaces_member_and_collection(cm) -> None:
    assert {("GET", "/"), ("GET", "/api/teas"), ("POST", "/api/teas"), ("GET", "/api/teas/:slug"), ("PATCH", "/api/teas/:slug"),
            ("PUT", "/api/teas/:slug"), ("DELETE", "/api/teas/:slug"), ("POST", "/api/teas/:slug/steep"), ("GET", "/api/teas/seasonal"),
            ("GET", "/api/teas/pairings"), ("GET", "/api/teas/:tea_slug/reviews"), ("POST", "/api/teas/:tea_slug/reviews"),
            ("DELETE", "/api/teas/:tea_slug/reviews/:id"), ("GET", "/api/basket"), ("PATCH", "/api/basket"), ("PUT", "/api/basket")} <= _routes(cm)
    assert not {("GET", "/api/teas/new"), ("GET", "/api/teas/:slug/edit"), ("GET", "/api/teas/:tea_slug/reviews/:id")} & _routes(cm)


def test_rails_verb_routes_scopes_match_and_devise(cm) -> None:
    assert {("GET", "/api/v2/blends/:id"), ("POST", "/api/v2/blends"), ("GET", "/admin/reports"), ("POST", "/admin/reports"),
            ("GET", "/admin/debug"), ("POST", "/users/login"), ("DELETE", "/users/sign_out"), ("POST", "/users")} <= _routes(cm)
    assert ("GET", "/commented/out") not in _routes(cm)


def test_rails_route_points_at_the_controller_action(cm) -> None:
    files = _files(cm)
    steep = next(r for r in cm.routes if r["p"] == "/api/teas/:slug/steep")
    assert files[steep["f"]] == f"{R}/app/controllers/api/teas_controller.rb" and steep["l"] == 14
    assert steep["t"] == "Steep teas"
    blend = next(r for r in cm.routes if r["p"] == "/api/v2/blends/:id")  # no such controller: the route stays in routes.rb
    assert files[blend["f"]] == f"{R}/config/routes.rb" and blend["l"] == 20
    login = next(r for r in cm.routes if r["p"] == "/users/login")
    assert files[login["f"]] == f"{R}/app/controllers/sessions_controller.rb"


def test_sinatra_and_grape_routes(cm) -> None:
    assert {("GET", "/health"), ("POST", "/kettles/:id/boil"), ("DELETE", "/kettles/:id"), ("GET", "/api/v1/leaves"),
            ("GET", "/api/v1/leaves/:id"), ("PUT", "/api/v1/leaves/:id/:grade")} <= _routes(cm)


def test_no_routes_from_specs_or_vendor(cm) -> None:
    paths = {p for _, p in _routes(cm)}
    assert not paths & {"/spec-only", "/vendored"}
    assert len(cm.routes) == 30


def test_services_are_gemfile_folders(cm) -> None:
    assert cm.services == ["kettle", "leafapi", R]


def test_require_relative_and_autoload_imports(cm) -> None:
    idx = {f: i for i, f in enumerate(_files(cm))}
    links = {(a, b) for a, b, _, _ in cm.links}
    teas = idx[f"{R}/app/controllers/api/teas_controller.rb"]
    assert (idx[f"{R}/app/services/brew_queue.rb"], idx[f"{R}/lib/brew_mailer.rb"]) in links
    assert (teas, idx[f"{R}/app/models/tea.rb"]) in links
    assert (teas, idx[f"{R}/app/services/brew_queue.rb"]) in links
    assert (idx[f"{R}/config/routes.rb"], teas) in links


def test_externals(cm) -> None:
    x = {f["p"]: set(f["x"]) for f in cm.files}
    assert "postgres" in x[f"{R}/app/models/tea.rb"]
    assert "sqs" in x[f"{R}/app/services/brew_queue.rb"]
    assert "ses" in x[f"{R}/lib/brew_mailer.rb"]
    assert {"cache", "postgres"} <= x[f"{R}/app/controllers/api/teas_controller.rb"]
    assert "kafka" in x["kettle/app.rb"]
    assert "mysql" in x["leafapi/models/leaf.rb"]
    assert not {"postgres", "mysql"} & x["kettle/app.rb"]  # sqlite is a file next to the app, not a separate system


def _data(base: Path) -> dict:
    cm = code_scan.scan([base])
    return {"base": str(base), "files": cm.files, "routes": cm.routes, "services": cm.services}


def _flow(data: dict, method: str, path: str) -> list[tuple]:
    i = next(i for i, r in enumerate(data["routes"]) if r["m"] == method and r["p"] == path)
    steps = ruby.flows(data, Path(data["base"]), i)
    assert steps is not None
    return [(s[0], s[1], s[2], s[5], s[6]) for s in steps]


def test_rails_flow_with_conditions_rescue_and_one_level_calls(tmp_path: Path) -> None:
    data = _data(write(tmp_path, WREN))
    assert _flow(data, "POST", "/api/teas/:slug/steep") == [
        (0, "call", "load_tea()", 0, ""),
        (1, "sql", "Postgres: SELECT Tea", 0, ""),
        (0, "sql", "Postgres: UPDATE Tea", 0, ""),
        (0, "call", "enqueue()", 0, ""),
        (1, "aws", "SQS: SendMessage", 0, ""),
        (0, "cache", "Cache: SET", 2, "a service error happens"),
    ]
    assert _flow(data, "GET", "/api/teas") == [(0, "sql", "Postgres: SELECT Tea", 0, ""), (0, "sql", "Postgres: SELECT Tea", 1, "limit is set")]
    assert _flow(data, "POST", "/api/teas") == [(0, "sql", "Postgres: INSERT Tea", 0, ""), (0, "call", "tea_params()", 0, ""),
                                               (0, "sql", "Postgres: INSERT Review", 1, "tea is not blend")]
    assert _flow(data, "POST", "/users/login") == [(0, "sql", "Postgres: SELECT User", 0, ""), (0, "cache", "Cache: SET", 0, "")]


def test_sinatra_and_grape_flows(tmp_path: Path) -> None:
    data = _data(write(tmp_path, WREN))
    assert _flow(data, "POST", "/kettles/:id/boil") == [(0, "kafka", "Kafka: send to kettle-events", 0, "")]
    assert _flow(data, "PUT", "/api/v1/leaves/:id/:grade") == [(0, "sql", "MySQL: SELECT by id Leaf", 0, "")]


def test_end_blocks_ignore_modifiers_strings_comments_and_heredocs() -> None:
    src = '''class A
  def a
    x = 1 if y # an end in a comment
    s = "an end in a string"
    q = <<~SQL
      select 1 where x = 'end'
    SQL
    while z do
      z -= 1
    end
    return unless w
    v = if k then 1 else 2 end
    [1].each do |i|
      i
    end
  end
  def b = 2
  def c; end
end
'''
    blocks = rubytext.blocks(rubytext.blank(src, True))
    by_kw = sorted((src[b.pos:b.pos + 6].split()[0], rubytext.line(src, b.pos), rubytext.line(src, b.end)) for b in blocks)
    assert by_kw == [("class", 1, 19), ("def", 2, 16), ("def", 18, 18), ("do", 13, 15), ("if", 12, 12), ("while", 8, 10)]


def test_database_gem_is_read_inside_the_repository_only(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("gem 'mysql2'\n")  # outside the scanned repository
    repo = write(tmp_path / "repo", {"Dockerfile": "FROM ruby\n", "app/models/cup.rb": "class Cup < ApplicationRecord\nend\n"})
    x = set(code_scan.scan([repo]).files[0]["x"])
    assert "postgres" in x and "mysql" not in x


def test_workflow_stream_and_kafka_clients() -> None:
    assert {"sfn", "kinesis", "kafka"} <= ruby.externals("require 'aws-sdk-states'\nk = Aws::Kinesis::Client.new\nKarafka.producer.produce_sync(topic: 'x')\n")
