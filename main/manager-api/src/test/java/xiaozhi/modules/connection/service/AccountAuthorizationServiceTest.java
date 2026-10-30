package xiaozhi.modules.connection.service;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import com.sun.net.httpserver.HttpServer;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import xiaozhi.common.constant.Constant;
import xiaozhi.common.exception.RenException;
import xiaozhi.modules.connection.entity.ServiceConnectionEntity;
import xiaozhi.modules.sys.service.SysParamsService;

class AccountAuthorizationServiceTest {
    @Test void publicStatusStripsTokensAndUntrustedErrorText() {
        var result = AccountAuthorizationService.publicStatus(Map.of("state","signed_in","email","test@example.invalid","planType","plus","accessToken","NEVER","error","PRIVATE"));
        assertEquals(Map.of("state","signed_in","email","test@example.invalid","planType","plus"), result);
    }
    @Test void maliciousLoginUrlIsRejected() {
        assertThrows(RenException.class, () -> AccountAuthorizationService.publicStatus(Map.of("state","pending","userCode","ABCD-1234","verificationUrl","https://evil.invalid")));
    }
    @Test void invalidStatusIsRejected() {
        assertThrows(RenException.class, () -> AccountAuthorizationService.publicStatus(Map.of("state","authorized")));
    }
    @Test void actualLoopbackHttpBindsConnectionToAuthenticatedOwnerAndUsesServerSecret() throws Exception {
        var connections = mock(ServiceConnectionService.class); var params = mock(SysParamsService.class);
        var connection = new ServiceConnectionEntity(); connection.setKind("chatgpt_realtime"); connection.setRuntimeId("local");
        when(connections.owned("connection",123L)).thenReturn(connection);
        when(params.getValue(Constant.SERVER_SECRET,true)).thenReturn("synthetic-secret-longer-than-24");
        AtomicReference<String> received = new AtomicReference<>(); AtomicReference<String> token = new AtomicReference<>();
        var server = HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);
        server.createContext("/internal/codex/accounts/start", exchange -> {
            received.set(new String(exchange.getRequestBody().readAllBytes(),StandardCharsets.UTF_8));
            token.set(exchange.getRequestHeaders().getFirst("Authorization"));
            byte[] body="{\"state\":\"pending\",\"verificationUrl\":\"https://auth.openai.com/codex/device\",\"userCode\":\"ABCD-1234\",\"accessToken\":\"NEVER\"}".getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200,body.length); exchange.getResponseBody().write(body); exchange.close();
        });
        server.start();
        try {
            var json = new ObjectMapper();
            var service = new AccountAuthorizationService(connections,params,json,"http://127.0.0.1:"+server.getAddress().getPort());
            var result=service.account("connection",123L,"start");
            assertEquals("pending",result.get("state")); assertFalse(result.containsKey("accessToken"));
            assertEquals(Map.of("ownerId","123","connectionId","connection"),json.readValue(received.get(),Map.class));
            assertEquals("Bearer synthetic-secret-longer-than-24",token.get());
            verify(connections).owned("connection",123L);
        } finally { server.stop(0); }
    }
    @Test void ownerDenialDoesNotReachRuntime() {
        var connections=mock(ServiceConnectionService.class); when(connections.owned("other",123L)).thenThrow(new RenException("denied"));
        var params=mock(SysParamsService.class);
        var service=new AccountAuthorizationService(connections,params,new ObjectMapper(),"http://127.0.0.1:9");
        assertThrows(RenException.class,()->service.account("other",123L,"start")); verifyNoInteractions(params);
    }
}
