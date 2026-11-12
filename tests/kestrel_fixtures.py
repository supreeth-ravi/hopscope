"""Small invented Kestrel Coffee projects for the Java/Kotlin, C# and frontend scanners. Every name is made up."""
from __future__ import annotations

from pathlib import Path

J = "roaster-service/src/main/java/coffee/kestrel/roaster"

ROASTER = {
    "roaster-service/pom.xml": "<project><dependencies><dependency><artifactId>postgresql</artifactId></dependency></dependencies></project>\n",
    "roaster-service/target/classes/Junk.java": "@RestController class Junk { @GetMapping(\"/junk\") void j() {} }\n",
    f"{J}/RoasterApplication.java": ("package coffee.kestrel.roaster;\n\nimport org.springframework.boot.autoconfigure.SpringBootApplication;\n\n"
                                     "@SpringBootApplication\npublic class RoasterApplication {\n    public static void main(String[] args) {}\n}\n"),
    f"{J}/web/RoastController.java": '''package coffee.kestrel.roaster.web;

import coffee.kestrel.roaster.service.RoastService;
import coffee.kestrel.roaster.model.*;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/roasts")
public class RoastController {
    private final RoastService roastService;

    public RoastController(RoastService roastService) {
        this.roastService = roastService;
    }

    /** List the roasts planned this week. */
    @GetMapping
    public List<Roast> list() {
        return roastService.planned();
    }

    @GetMapping("/{id}")
    public Roast get(@PathVariable long id) {
        return roastService.find(id);
    }

    @PostMapping(value = "/{id}/start", produces = "application/json")
    public Roast start(@PathVariable long id) {
        return this.roastService.start(id);
    }

    @RequestMapping(path = "/batch", method = {RequestMethod.PUT, RequestMethod.PATCH})
    public void batch(@RequestBody Batch batch) {
        // @GetMapping("/commented-out") is not a route
    }

    @DeleteMapping("/{id}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void cancel(@PathVariable("id") long id) {
        roastService.cancel(id);
    }
}
''',
    f"{J}/service/RoastService.java": '''package coffee.kestrel.roaster.service;

import coffee.kestrel.roaster.model.Roast;
import coffee.kestrel.roaster.repository.RoastRepository;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.web.client.RestTemplate;
import software.amazon.awssdk.services.sqs.SqsClient;

@Service
public class RoastService {
    private final RoastRepository roasts;
    private final SqsClient sqs;
    private final RestTemplate rest;
    private final StringRedisTemplate redis;
    private final KafkaTemplate<String, String> kafka;

    public RoastService(RoastRepository roasts, SqsClient sqs, RestTemplate rest, StringRedisTemplate redis, KafkaTemplate<String, String> kafka) {
        this.roasts = roasts;
        this.sqs = sqs;
        this.rest = rest;
        this.redis = redis;
        this.kafka = kafka;
    }

    public List<Roast> planned() {
        return roasts.findAll();
    }

    public Roast find(long id) {
        return roasts.findById(id).orElseThrow();
    }

    public Roast start(long id) {
        Roast roast = find(id);
        if (roast.isReady()) {
            kafka.send("roast-events", "started");
        } else {
            rest.postForObject("http://loyalty-api/api/points", roast, String.class);
        }
        try {
            sqs.sendMessage(r -> r.queueUrl("kestrel-roasts.fifo"));
        } catch (SdkException e) {
            redis.opsForValue().set("roast:" + id, "pending");
        }
        return roasts.save(roast);
    }

    public void cancel(long id) {
        roasts.deleteById(id);
    }
}
''',
    f"{J}/repository/RoastRepository.java": ("package coffee.kestrel.roaster.repository;\n\nimport coffee.kestrel.roaster.model.Roast;\n"
                                             "import org.springframework.data.jpa.repository.JpaRepository;\n\n"
                                             "public interface RoastRepository extends JpaRepository<Roast, Long> {\n}\n"),
    f"{J}/model/Roast.java": "package coffee.kestrel.roaster.model;\n\n@Entity\npublic class Roast {\n    boolean isReady() { return true; }\n}\n",
    f"{J}/model/Batch.java": "package coffee.kestrel.roaster.model;\n\npublic record Batch(long id) {}\n",
    f"{J}/model/Unused.java": "package coffee.kestrel.roaster.model;\n\npublic class Unused {}\n",
    f"{J}/client/LoyaltyClient.java": ('package coffee.kestrel.roaster.client;\n\n@FeignClient(name = "loyalty")\npublic interface LoyaltyClient {\n'
                                       '    @PostMapping("/api/points")\n    void addPoints();\n}\n'),
    "roaster-service/src/main/kotlin/coffee/kestrel/roaster/web/BeanCatalogController.kt": '''package coffee.kestrel.roaster.web

import org.springframework.web.bind.annotation.*

@RestController
@RequestMapping("/api/beans")
class BeanCatalogController(private val catalog: CatalogService) {
    @GetMapping
    fun all(): List<String> = catalog.all()

    @GetMapping(["/{origin}"])
    suspend fun byOrigin(@PathVariable origin: String) = catalog.byOrigin(origin)
}
''',
    "roaster-service/src/main/kotlin/coffee/kestrel/roaster/web/CatalogService.kt": ("package coffee.kestrel.roaster.web\n\nclass CatalogService {\n"
                                                                                       "    fun all() = listOf<String>()\n    fun byOrigin(o: String) = listOf<String>()\n}\n"),
    "roaster-service/src/test/java/coffee/kestrel/roaster/RoastControllerTest.java": "@RestController class T { @GetMapping(\"/t\") void t() {} }\n",
}

L = "loyalty-api/Kestrel.Loyalty"

LOYALTY = {
    f"{L}/Kestrel.Loyalty.csproj": ('<Project Sdk="Microsoft.NET.Sdk.Web"><ItemGroup>'
                                    '<PackageReference Include="Microsoft.EntityFrameworkCore.SqlServer" /></ItemGroup></Project>\n'),
    f"{L}/obj/Debug/Generated.cs": "[ApiController][Route(\"junk\")] public class JunkController { [HttpGet] public void J() {} }\n",
    f"{L}/Controllers/MembersController.cs": '''using Kestrel.Loyalty.Services;
using Microsoft.AspNetCore.Mvc;

namespace Kestrel.Loyalty.Controllers;

[ApiController]
[Route("api/[controller]")]
public class MembersController : ControllerBase
{
    private readonly IMemberService _members;

    public MembersController(IMemberService members)
    {
        _members = members;
    }

    /// <summary>Every loyalty member.</summary>
    [HttpGet]
    public async Task<IActionResult> List() => Ok(await _members.AllAsync());

    [HttpGet("{id:int}")]
    public async Task<IActionResult> GetById(int id)
    {
        var member = await _members.FindAsync(id);
        if (member == null)
        {
            return NotFound();
        }
        return Ok(member);
    }

    [HttpPost("{id}/points")]
    [Authorize]
    public async Task<IActionResult> AddPoints(int id, PointsRequest req)
    {
        await _members.AddPointsAsync(id, req.Points);
        return Ok();
    }

    [HttpDelete, Route("{id}")]
    public IActionResult Remove(int id) => NoContent();

    [Route("/ping")]
    [HttpGet]
    public string Ping() => "pong";
}
''',
    f"{L}/Services/MemberService.cs": '''using Amazon.SimpleEmail;
using Kestrel.Loyalty.Data;
using StackExchange.Redis;

namespace Kestrel.Loyalty.Services
{
    public interface IMemberService
    {
        Task<List<Member>> AllAsync();
        Task<Member> FindAsync(int id);
        Task AddPointsAsync(int id, int points);
    }

    public class MemberService : IMemberService
    {
        private readonly LoyaltyDbContext _db;
        private readonly IAmazonSimpleEmailService _ses;
        private readonly IDatabase _cache;
        private readonly HttpClient _http;

        public MemberService(LoyaltyDbContext db, IAmazonSimpleEmailService ses, IConnectionMultiplexer redis, HttpClient http)
        {
            _db = db;
            _ses = ses;
            _cache = redis.GetDatabase();
            _http = http;
        }

        public Task<List<Member>> AllAsync() => _db.Members.ToListAsync();

        public async Task<Member> FindAsync(int id)
        {
            var cached = await _cache.StringGetAsync($"member:{id}");
            return await _db.Members.FindAsync(id);
        }

        public async Task AddPointsAsync(int id, int points)
        {
            var member = await _db.Members.FindAsync(id);
            member.Points += points;
            await _db.SaveChangesAsync();
            try
            {
                await _ses.SendEmailAsync(new SendEmailRequest());
            }
            catch (AmazonSimpleEmailServiceException)
            {
                await _http.PostAsync("http://roaster-service/api/roasts/1/start", null);
            }
        }
    }
}
''',
    f"{L}/Data/LoyaltyDbContext.cs": ("using Microsoft.EntityFrameworkCore;\n\nnamespace Kestrel.Loyalty.Data;\n\n"
                                      "public class LoyaltyDbContext : DbContext\n{\n    public DbSet<Member> Members { get; set; }\n}\n\n"
                                      "public class Member\n{\n    public int Points { get; set; }\n}\n"),
    f"{L}/Program.cs": '''using Kestrel.Loyalty.Data;
using Confluent.Kafka;

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

app.MapGet("/health", () => "ok");
var rewards = app.MapGroup("/api/rewards");
rewards.MapGet("/", async (LoyaltyDbContext db) => await db.Members.ToListAsync());
rewards.MapPost("/{code}/redeem", async (string code, IProducer<string, string> producer) =>
{
    await producer.ProduceAsync("rewards-redeemed", new Message<string, string>());
    return Results.Accepted();
});
var admin = rewards.MapGroup("admin");
admin.MapDelete("{code}", RemoveReward);
app.Run();
''',
    "loyalty-api/Kestrel.Loyalty.sln": "Microsoft Visual Studio Solution File\n",
    "loyalty-api/tests/Kestrel.Loyalty.Tests/MembersControllerTests.cs": "[ApiController] public class TController { [HttpGet(\"t\")] public void T() {} }\n",
}

STOREFRONT = {
    "storefront/package.json": '{"name": "storefront", "dependencies": {"react": "18.3.1", "axios": "1.7.7"}}\n',
    "storefront/src/api.ts": '''import axios from "axios";

const API_BASE = import.meta.env.VITE_API_BASE;
export const api = axios.create({ baseURL: API_BASE });

export async function loadRoasts() {
  const res = await fetch(`${API_BASE}/api/roasts`);
  return res.json();
}

export const startRoast = (id: number) => api.post(`/api/roasts/${id}/start`);
''',
    "storefront/src/pages/Members.tsx": '''import { api } from "../api";

export function Members({ memberId }: { memberId: number }) {
  const load = () => axios.get("/api/members/" + memberId);
  const remove = () => fetch(`/api/members/${memberId}`, { method: "DELETE" });
  const redeem = (code: string) => api.post(`/api/rewards/${code}/redeem`);
  const gone = () => api.get("/api/gift-cards");
  const map = new Map(); map.get("/not-a-call");
  return <button onClick={load}>Load</button>;
}
''',
}


def write(base: Path, *projects: dict[str, str]) -> Path:
    for files in projects:
        for rel, text in files.items():
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    return base
